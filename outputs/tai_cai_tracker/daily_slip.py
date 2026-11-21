"""Read-only daily paper slip built from already-frozen ledger records.

This module never runs the model, never fetches a source, and never writes a
file. It only reads three frozen record types from ``paper_ledger.jsonl``:

* ``prediction`` - the frozen model distribution;
* ``all_market_snapshot`` - the official fixed prizes linked by
  ``snapshot["prediction_hash"] == prediction["content_hash"]``;
* ``all_market_settlement`` - used only to label an already-settled replay.

SPF probabilities come from ``model["blended_outcomes"]``. That frozen field is
the SPF distribution stored in the linked snapshot; deriving SPF from the
scoreline matrix instead would return ``model["model_outcomes"]`` (the pure
Poisson component), which differs from the frozen blend by the market weight.
HHAD, TTG, CRS and HAFU are derived here from the frozen distributions exactly
as the all-market snapshot does, so a probability shown on this slip can be
reconciled against the same match's frozen record.

The output is paper analysis only. It is not a purchase instruction, a funding
recommendation, or evidence that any outcome is likely to occur.
"""

from __future__ import annotations

import unicodedata
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

DEFAULT_LEDGER_PATH = Path(__file__).resolve().parent / "data" / "paper_ledger.jsonl"
LOCAL_ZONE = ZoneInfo("Asia/Shanghai")

OUTCOME_OPTIONS = ("home_win", "draw", "away_win")
OUTCOME_LABELS = {"home_win": "主胜", "draw": "平", "away_win": "客胜"}
# Visual padding: 主胜/客胜 already occupy four display columns; 平 needs two.
OUTCOME_DISPLAY = {"home_win": "主胜", "draw": "平  ", "away_win": "客胜"}

TTG_OPTIONS = ("0", "1", "2", "3", "4", "5", "6", "7+")
TTG_LABELS = {option: (f"{option}球" if option != "7+" else "7+") for option in TTG_OPTIONS}

HAFU_OPTIONS = ("HH", "HD", "HA", "DH", "DD", "DA", "AH", "AD", "AA")
_HAFU_SYMBOL = {"home_win": "H", "draw": "D", "away_win": "A"}

# Official correct-score options are the numeric home:away keys. The three
# H_OTHER/D_OTHER/A_OTHER buckets are not single scorelines and are excluded.
def _is_explicit_score(option: str) -> bool:
    if not isinstance(option, str) or option.count(":") != 1:
        return False
    home, away = option.split(":")
    return home.isdigit() and away.isdigit()


RULE_LINES = (
    "只记录 SPF 和 HHAD：三选项玩法返奖率 88.4%，总进球/半全场 81.8%，比分 73.3%",
    "EV 上限 30%：实测 EV>60% 的 11 注模型高估 19pp，实际 ROI −80.9%",
)

EV_FLOOR = 0.0
EV_CEILING = 0.30
EV_PRIORITY_TIER = 0.15
STAKE_PER_BET = 2
PARLAY_MIN_BUDGET = 4


# ---------------------------------------------------------------------------
# Small numeric / formatting helpers
# ---------------------------------------------------------------------------


def _as_float(value: Any) -> float | None:
    """Convert a ledger value (str, Decimal, int, float) to float or None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(Decimal(str(value)))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _as_int(value: Any) -> int | None:
    number = _as_float(value)
    if number is None or not number.is_integer():
        return None
    return int(number)


def _display_width(text: str) -> int:
    return sum(
        2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
        for character in text
    )


def _pad(text: str, width: int, align: str = "left") -> str:
    gap = max(0, width - _display_width(text))
    if align == "right":
        return " " * gap + text
    return text + " " * gap


def _percent(value: float, places: int = 1) -> str:
    return f"{value * 100:.{places}f}%"


def _ev_text(value: float) -> str:
    text = f"{value * 100:+.1f}%"
    return text.replace("-", "−")


def compute_ev(probability: float, odds: float) -> float:
    """Expected return per unit staked at the official fixed prize."""
    return probability * odds - 1.0


def parse_timestamp(value: Any) -> datetime | None:
    """Parse a timezone-aware ISO timestamp and convert it to Asia/Shanghai."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(LOCAL_ZONE)


# ---------------------------------------------------------------------------
# Probability derivation
# ---------------------------------------------------------------------------


def normalize_scorelines(model: Mapping[str, Any]) -> dict[str, float]:
    """Return ``{"home:away": probability}`` normalized to sum to one."""
    lines = model.get("scorelines")
    if not isinstance(lines, list) or not lines:
        raise ValueError("model scorelines are unavailable")
    totals: dict[str, float] = {}
    for line in lines:
        if not isinstance(line, Mapping):
            raise ValueError("model scorelines are invalid")
        home_goals = line.get("home_goals")
        away_goals = line.get("away_goals")
        if (
            isinstance(home_goals, bool)
            or isinstance(away_goals, bool)
            or not isinstance(home_goals, int)
            or not isinstance(away_goals, int)
            or home_goals < 0
            or away_goals < 0
        ):
            raise ValueError("model scoreline goals are invalid")
        probability = _as_float(line.get("probability"))
        if probability is None or not probability >= 0:
            raise ValueError("model scoreline probability is invalid")
        key = f"{home_goals}:{away_goals}"
        totals[key] = totals.get(key, 0.0) + probability
    mass = sum(totals.values())
    if mass <= 0:
        raise ValueError("model scorelines carry no probability mass")
    return {key: value / mass for key, value in totals.items()}


def spf_probabilities(model: Mapping[str, Any]) -> dict[str, float]:
    """Frozen SPF distribution.

    ``blended_outcomes`` is the SPF distribution frozen beside the scoreline
    matrix. It is preferred over the scoreline aggregate because the linked
    snapshot's SPF market uses the same blend. Legacy records without it fall
    back to the scoreline aggregate.
    """
    blended = model.get("blended_outcomes")
    if isinstance(blended, Mapping):
        values = {option: _as_float(blended.get(option)) for option in OUTCOME_OPTIONS}
        if all(value is not None and value >= 0 for value in values.values()):
            total = sum(values.values())
            if total > 0:
                return {option: value / total for option, value in values.items()}
    scores = normalize_scorelines(model)
    return _outcome_from_scorelines(scores)


def _outcome_from_scorelines(scores: Mapping[str, float], handicap: int = 0) -> dict[str, float]:
    result = {option: 0.0 for option in OUTCOME_OPTIONS}
    for key, probability in scores.items():
        home, away = (int(part) for part in key.split(":"))
        adjusted = home + handicap
        if adjusted > away:
            result["home_win"] += probability
        elif adjusted == away:
            result["draw"] += probability
        else:
            result["away_win"] += probability
    return result


def hhad_probabilities(scores: Mapping[str, float], handicap: int) -> dict[str, float]:
    """Three-way handicap distribution for a whole-number home handicap."""
    return _outcome_from_scorelines(scores, handicap)


def ttg_probabilities(scores: Mapping[str, float]) -> dict[str, float]:
    """Total-goals distribution in the official eight buckets."""
    result = {option: 0.0 for option in TTG_OPTIONS}
    for key, probability in scores.items():
        home, away = (int(part) for part in key.split(":"))
        total = home + away
        result[str(total) if total <= 6 else "7+"] += probability
    return result


def crs_probabilities(
    scores: Mapping[str, float], official_options: Sequence[str]
) -> tuple[dict[str, float], float]:
    """Correct-score probabilities for official explicit scores and residual mass.

    The residual is probability carried by scorelines that the official CRS
    table does not offer (for example 6:0 or 4:3). It is reported, never
    redistributed into an "other" option.
    """
    offered = {option for option in official_options if _is_explicit_score(option)}
    probabilities = {option: scores[option] for option in offered if option in scores}
    held = sum(probabilities.values())
    return probabilities, max(0.0, 1.0 - held)


def hafu_probabilities(model: Mapping[str, Any]) -> dict[str, float]:
    """Half-time/full-time distribution mapped to the nine official two-letter codes."""
    source = model.get("half_time_full_time")
    if not isinstance(source, Mapping):
        raise ValueError("model half_time_full_time is unavailable")
    result = {option: 0.0 for option in HAFU_OPTIONS}
    for key, raw_probability in source.items():
        if not isinstance(key, str) or key.count(":") != 1:
            raise ValueError("model half_time_full_time key is invalid")
        half_time, full_time = key.split(":")
        if half_time not in _HAFU_SYMBOL or full_time not in _HAFU_SYMBOL:
            raise ValueError("model half_time_full_time outcome is invalid")
        probability = _as_float(raw_probability)
        if probability is None or probability < 0:
            raise ValueError("model half_time_full_time probability is invalid")
        result[_HAFU_SYMBOL[half_time] + _HAFU_SYMBOL[full_time]] += probability
    if not any(result.values()):
        raise ValueError("model half_time_full_time carries no probability mass")
    return result


# ---------------------------------------------------------------------------
# Ledger projection
# ---------------------------------------------------------------------------


def _market_prizes(snapshot: Mapping[str, Any], market: str) -> dict[str, str]:
    section = snapshot.get("markets", {}).get(market)
    if not isinstance(section, Mapping) or section.get("status") != "frozen":
        return {}
    prizes = section.get("official_fixed_prizes") or section.get("fixed_prizes") or {}
    if not isinstance(prizes, Mapping):
        return {}
    return {str(option): str(odds) for option, odds in prizes.items() if odds is not None}


def _handicap(snapshot: Mapping[str, Any]) -> int | None:
    section = snapshot.get("markets", {}).get("hhad")
    if not isinstance(section, Mapping):
        return None
    return _as_int(section.get("home_handicap"))


def _linked_records(records: Sequence[Mapping[str, Any]]):
    snapshots: dict[str, Mapping[str, Any]] = {}
    settlements: dict[str, Mapping[str, Any]] = {}
    for record in records:
        record_type = record.get("record_type")
        if record_type == "all_market_snapshot":
            prediction_hash = record.get("prediction_hash")
            if isinstance(prediction_hash, str):
                snapshots[prediction_hash] = record
        elif record_type == "all_market_settlement":
            snapshot_hash = record.get("snapshot_hash")
            if isinstance(snapshot_hash, str):
                settlements[snapshot_hash] = record
    return snapshots, settlements


def _primary_predictions(records: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """One primary prediction per match identity (comparison records excluded)."""
    chosen: dict[tuple[str, str], Mapping[str, Any]] = {}
    for record in records:
        if record.get("record_type") != "prediction":
            continue
        if record.get("status") not in (None, "frozen"):
            continue
        if record.get("prediction_role") == "comparison":
            continue
        match_id = record.get("match_id")
        kickoff_at = record.get("kickoff_at")
        if not isinstance(match_id, str) or not isinstance(kickoff_at, str):
            continue
        key = (match_id, kickoff_at)
        current = chosen.get(key)
        if current is None or str(record.get("captured_at", "")) >= str(
            current.get("captured_at", "")
        ):
            chosen[key] = record
    return list(chosen.values())


def _build_match(
    prediction: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    settlement: Mapping[str, Any] | None,
    kickoff: datetime,
) -> dict[str, Any] | None:
    model = prediction.get("model")
    if not isinstance(model, Mapping):
        return None
    handicap = _handicap(snapshot)
    if handicap is None:
        return None
    prizes = {market: _market_prizes(snapshot, market) for market in ("spf", "hhad", "ttg", "crs", "hafu")}
    if not prizes["spf"] or not prizes["hhad"]:
        return None

    scores = normalize_scorelines(model)
    crs, crs_residual = crs_probabilities(scores, tuple(prizes["crs"]))
    context = prediction.get("match_context")
    context = context if isinstance(context, Mapping) else {}

    settled = None
    if settlement is not None:
        final_score = settlement.get("final_score")
        if isinstance(final_score, Mapping):
            home = final_score.get("home")
            away = final_score.get("away")
            if isinstance(home, int) and isinstance(away, int):
                settled = (home, away)
        if settled is None:
            settled = (None, None)

    return {
        "match_id": prediction.get("match_id"),
        "home_team": context.get("home_team") or "主队",
        "away_team": context.get("away_team") or "客队",
        "kickoff": kickoff,
        "handicap": handicap,
        "spf": spf_probabilities(model),
        "hhad": hhad_probabilities(scores, handicap),
        "ttg": ttg_probabilities(scores),
        "crs": crs,
        "crs_residual": crs_residual,
        "hafu": hafu_probabilities(model),
        "prizes": prizes,
        "settled": settled,
    }


def _candidate(match: Mapping[str, Any]) -> dict[str, Any] | None:
    """Highest-priority selectable option for one match, if any."""
    best: dict[str, Any] | None = None
    for market_rank, market in enumerate(("spf", "hhad")):
        probabilities = match[market]
        prizes = match["prizes"][market]
        for option_rank, option in enumerate(OUTCOME_OPTIONS):
            probability = probabilities.get(option)
            odds = _as_float(prizes.get(option))
            if probability is None or odds is None or odds <= 0:
                continue
            ev = compute_ev(probability, odds)
            if not (EV_FLOOR < ev < EV_CEILING):
                continue
            tier = 0 if ev >= EV_PRIORITY_TIER else 1
            key = (tier, -ev, market_rank, option_rank)
            if best is None or key < best["priority_key"]:
                best = {
                    "match_id": match["match_id"],
                    "kickoff": match["kickoff"],
                    "home_team": match["home_team"],
                    "away_team": match["away_team"],
                    "handicap": match["handicap"],
                    "market": market.upper(),
                    "market_key": market,
                    "option": option,
                    "option_rank": option_rank,
                    "probability": probability,
                    "odds": odds,
                    "odds_text": prizes[option],
                    "ev": ev,
                    "priority_key": key,
                }
    return best


def _selection_order(selection: Mapping[str, Any]):
    return (
        selection["priority_key"][0],
        selection["priority_key"][1],
        selection["kickoff"],
        str(selection["match_id"]),
        selection["priority_key"][2],
        selection["priority_key"][3],
    )


def build_daily_slip(
    records: Sequence[Mapping[str, Any]],
    target_date: str | date | None = None,
    budget: int = 10,
    now: datetime | None = None,
    ledger_path: str | Path | None = None,
) -> dict[str, Any]:
    """Build the read-only daily paper slip.

    An explicit ``target_date`` is a replay: every frozen primary prediction
    kicked off on that local date is shown. Without it, the command uses today
    (Asia/Shanghai) and shows only fixtures whose kickoff is still in the
    future.
    """
    if isinstance(budget, bool) or not isinstance(budget, int):
        raise ValueError("budget must be an integer number of yuan")
    if budget < 0:
        raise ValueError("budget cannot be negative")

    if now is None:
        now = datetime.now(LOCAL_ZONE)
    elif now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    else:
        now = now.astimezone(LOCAL_ZONE)

    if target_date is None:
        day = now.date()
        replay = False
    elif isinstance(target_date, datetime):
        raise ValueError("target_date must be a date or YYYY-MM-DD string")
    elif isinstance(target_date, date):
        day = target_date
        replay = True
    else:
        try:
            day = date.fromisoformat(str(target_date))
        except ValueError as error:
            raise ValueError("date must use YYYY-MM-DD") from error
        replay = True

    snapshots, settlements = _linked_records(records)

    matches: list[dict[str, Any]] = []
    missing_odds = 0
    missing_model = 0
    in_window = 0
    for prediction in _primary_predictions(records):
        kickoff = parse_timestamp(prediction.get("kickoff_at"))
        if kickoff is None or kickoff.date() != day:
            continue
        if not replay and kickoff <= now:
            continue
        in_window += 1
        model = prediction.get("model")
        if not isinstance(model, Mapping) or not isinstance(model.get("scorelines"), list):
            missing_model += 1
            continue
        snapshot = snapshots.get(prediction.get("content_hash"))
        if snapshot is None:
            missing_odds += 1
            continue
        settlement = settlements.get(snapshot.get("content_hash"))
        match = _build_match(prediction, snapshot, settlement, kickoff)
        if match is None:
            missing_odds += 1
            continue
        matches.append(match)

    matches.sort(key=lambda item: (item["kickoff"], str(item["match_id"])))

    candidates = [candidate for match in matches if (candidate := _candidate(match))]
    candidates.sort(key=_selection_order)
    affordable = budget // STAKE_PER_BET
    selections = candidates[:affordable]
    for selection in selections:
        selection["stake"] = STAKE_PER_BET
    total_stake = STAKE_PER_BET * len(selections)

    parlay = None
    if len(selections) >= 2 and budget >= PARLAY_MIN_BUDGET:
        first, second = selections[0], selections[1]
        if first["match_id"] != second["match_id"]:
            combined_odds = first["odds"] * second["odds"]
            parlay = {
                "legs": (first, second),
                "odds": combined_odds,
                "probability": first["probability"] * second["probability"],
                "payout": STAKE_PER_BET * combined_odds,
            }

    report = {
        "date": day,
        "budget": budget,
        "replay": replay,
        "matches": matches,
        "selections": selections,
        "total_stake": total_stake,
        "remaining": budget - total_stake,
        "parlay": parlay,
        "missing_odds": missing_odds,
        "missing_model": missing_model,
        "in_window": in_window,
        "ledger_path": str(ledger_path) if ledger_path is not None else None,
    }
    return report


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


SEPARATOR = "═" * 56
SUBSECTION = "─" * 56
SLIP_FOOTER_RULE = "─" * 52


def _option_lines(
    probabilities: Mapping[str, float],
    prizes: Mapping[str, str],
) -> list[str]:
    lines = []
    for option in OUTCOME_OPTIONS:
        probability = probabilities.get(option, 0.0)
        odds_text = prizes.get(option)
        odds = _as_float(odds_text)
        label = OUTCOME_DISPLAY[option]
        if odds is None:
            lines.append(f"   {label}  {_percent(probability)}  @—  EV    n/a")
            continue
        ev = compute_ev(probability, odds)
        marker = "  ←" if EV_FLOOR < ev < EV_CEILING else ""
        lines.append(
            f"   {label}  {_percent(probability)}  @{odds_text}  EV {_ev_text(ev):>6}{marker}"
        )
    return lines


def _ttg_lines(probabilities: Mapping[str, float], prizes: Mapping[str, str]) -> list[str]:
    lines = []
    for start in range(0, len(TTG_OPTIONS), 4):
        cells = []
        for option in TTG_OPTIONS[start : start + 4]:
            odds_text = prizes.get(option)
            cell = f"{TTG_LABELS[option]} {_percent(probabilities.get(option, 0.0))}"
            if odds_text is not None:
                cell += f"@{odds_text}"
            cells.append(cell)
        lines.append("   " + "   ".join(cells))
    return lines


def _crs_lines(
    probabilities: Mapping[str, float], prizes: Mapping[str, str]
) -> list[str]:
    ranked = sorted(
        (option for option in probabilities if option in prizes),
        key=lambda option: (-probabilities[option], option),
    )[:6]
    cells = [
        f"{option} {_percent(probabilities[option])}@{prizes[option]}"
        for option in ranked
    ]
    lines = []
    for start in range(0, len(cells), 3):
        lines.append("   " + "   ".join(cells[start : start + 3]))
    return lines


def _hafu_lines(probabilities: Mapping[str, float], prizes: Mapping[str, str]) -> list[str]:
    ranked = sorted(
        (option for option in HAFU_OPTIONS if option in prizes),
        key=lambda option: (-probabilities.get(option, 0.0), HAFU_OPTIONS.index(option)),
    )[:4]
    cells = [
        f"{option} {_percent(probabilities.get(option, 0.0))}@{prizes[option]}"
        for option in ranked
    ]
    return ["   " + "   ".join(cells)] if cells else []


def _match_block(index: int, match: Mapping[str, Any]) -> list[str]:
    kickoff = match["kickoff"].strftime("%H:%M")
    handicap = match["handicap"]
    header = (
        f"【{index}】{match['match_id']}  {match['home_team']} vs {match['away_team']}"
        f"   开赛 {kickoff}   让球 {handicap:+d}"
    )
    settled = match.get("settled")
    if settled is not None and settled[0] is not None:
        header += f"   已结算 {settled[0]}:{settled[1]}"

    prizes = match["prizes"]
    lines = [header, SUBSECTION]
    lines.append(" SPF 胜平负")
    lines.extend(_option_lines(match["spf"], prizes["spf"]))
    lines.append(f" HHAD 让球({handicap:+d})")
    lines.extend(_option_lines(match["hhad"], prizes["hhad"]))

    lines.append(" TTG 总进球            (只展示，不推荐)")
    lines.extend(_ttg_lines(match["ttg"], prizes["ttg"]))

    residual = match["crs_residual"]
    lines.append(
        f" CRS 比分 (前 6 高概率)  (只展示，不推荐)   残差 {_percent(residual)}"
    )
    lines.extend(_crs_lines(match["crs"], prizes["crs"]))

    lines.append(" HAFU 半全场            (只展示，不推荐)")
    lines.extend(_hafu_lines(match["hafu"], prizes["hafu"]))

    lines.append(
        " 自检  "
        f"SPF {sum(match['spf'].values()):.4f}   "
        f"HHAD {sum(match['hhad'].values()):.4f}   "
        f"TTG {sum(match['ttg'].values()):.4f}   "
        f"HAFU {sum(match['hafu'].values()):.4f}   "
        f"CRS {sum(match['crs'].values()):.4f} (残差 {_percent(residual, 2)})"
    )
    return lines


def _slip_option_label(selection: Mapping[str, Any]) -> str:
    label = OUTCOME_LABELS[selection["option"]]
    if selection["market_key"] == "hhad":
        return f"{label}({selection['handicap']:+d})"
    return label


def _slip_rows(selections: Sequence[Mapping[str, Any]]) -> list[str]:
    widths = (2, 8, 5, 12, 7, 7, 8, 6)
    aligns = ("right", "left", "left", "left", "right", "right", "right", "right")
    header = ("#", "场次", "玩法", "选项", "赔率", "概率", "EV", "金额")
    rows = ["  " + _row(header, widths, aligns)]
    for index, selection in enumerate(selections, start=1):
        cells = (
            str(index),
            str(selection["match_id"]),
            selection["market"],
            _slip_option_label(selection),
            selection["odds_text"],
            _percent(selection["probability"]),
            _ev_text(selection["ev"]),
            f"{selection['stake']} 元",
        )
        rows.append("  " + _row(cells, widths, aligns))
    return rows


def _row(cells: Sequence[str], widths: Sequence[int], aligns: Sequence[str]) -> str:
    return "  ".join(
        _pad(str(cell), width, align)
        for cell, width, align in zip(cells, widths, aligns)
    ).rstrip()


def format_daily_slip(report: Mapping[str, Any]) -> str:
    """Render one report produced by :func:`build_daily_slip`."""
    day = report["date"]
    day_text = day.isoformat() if isinstance(day, date) else str(day)

    if report["in_window"] == 0:
        return (
            f"{day_text} 没有可用场次。可能原因：当天赛程未开卖，或 prematch 任务还没跑。"
        )

    lines = [
        SEPARATOR,
        f" 每日纸面方案   {day_text}   预算 {report['budget']} 元   "
        f"可比 {len(report['matches'])} 场",
        SEPARATOR,
        "",
    ]
    lines.extend(f"  {rule}" for rule in RULE_LINES)
    lines.append("")

    for index, match in enumerate(report["matches"], start=1):
        lines.extend(_match_block(index, match))
        lines.append("")

    if report["missing_odds"]:
        lines.append(f"  另有 {report['missing_odds']} 场缺赔率，跳过。")
    if report["missing_model"]:
        lines.append(f"  另有 {report['missing_model']} 场缺模型分布，跳过。")
    if report["missing_odds"] or report["missing_model"]:
        lines.append("")

    lines.extend([SEPARATOR, " 今日注单", SEPARATOR])
    lines.extend(_slip_rows(report["selections"]))
    lines.append("  " + SLIP_FOOTER_RULE)
    lines.append(
        f"  共 {len(report['selections'])} 注 {report['total_stake']} 元"
        f"   剩 {report['remaining']} 元无可记录"
    )

    parlay = report["parlay"]
    if parlay is not None:
        first, second = parlay["legs"]
        lines.extend(
            [
                "",
                SEPARATOR,
                " 可选 2串1",
                SEPARATOR,
                f"  {first['match_id']} {first['market']} {_slip_option_label(first)}"
                f" × {second['match_id']} {second['market']} {_slip_option_label(second)}",
                f"  合并赔率 {parlay['odds']:.2f}   理论命中率 "
                f"{_percent(parlay['probability'])}   投入 {STAKE_PER_BET} 元命中 "
                f"{parlay['payout']:.2f} 元",
                "  串关仅作展示，不计入上方预算。",
                "  3串1 及以上不做：实测 8 注 0 中",
            ]
        )

    return "\n".join(lines)
