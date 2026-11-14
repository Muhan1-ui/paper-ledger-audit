"""Paper-only betting ledger and verifiable P&L report.

This module reads the immutable paper ledger (``paper_ledger.jsonl``) and never
writes it. Paper bets are stored in a separate mutable file
(``data/paper_bets.jsonl``), so the audit trail of frozen predictions and
settlements stays untouched.

INFORMATION BOUNDARY (must be preserved by any future change):

1. Bet selection (``generate_bets``) may read only frozen ``prediction``
   records and only the fields ``match_id``, ``content_hash``, ``kickoff_at``,
   ``match_context``, ``outcomes``, ``fixed_prizes`` and ``model``. ``model`` is
   read solely to tag a bet's ``probability_source`` provenance; it never
   affects which outcome is selected. It must never look at ``settlement`` /
   ``settlement_invalidation`` records, otherwise the choice of bet would be
   contaminated by the known result.
2. Settlement records are read only in the P&L stage
   (``settle_bets`` / ``report``) to decide whether an already frozen paper bet
   won or lost. They never influence which bet was generated.

Everything here is paper accounting. It is not a purchase instruction, a
funding recommendation, or a claim that any outcome is guaranteed.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

DEFAULT_LEDGER = Path("outputs/tai_cai_tracker/data/paper_ledger.jsonl")
DEFAULT_OUT = Path("outputs/tai_cai_tracker/data/paper_bets.jsonl")
# Measured bucket evidence: edges of 0-5 points are noise (122 bets, ROI -21.7%),
# while edges of 5-10 points are profitable (17 bets, ROI +54.2%). The default
# strategy therefore only takes bets whose best edge exceeds 5 points.
DEFAULT_THRESHOLD = 0.05
DEFAULT_OUTCOMES = ("home_win", "draw", "away_win")
PROBABILITY_PLACES = Decimal("0.0000000001")  # 10 decimal places
MONEY_PLACES = Decimal("0.01")

# Prediction fields read during bet generation. ``model`` is provenance only:
# it is used to label ``probability_source`` and never enters bet selection.
SELECTION_FIELDS = (
    "match_id",
    "content_hash",
    "kickoff_at",
    "match_context",
    "outcomes",
    "fixed_prizes",
    "model",
)

# A bet's probability is either a 0.7*European + 0.3*xG-Poisson blend or the
# de-vigged European price itself (cross-book spread, no model involved).
PROBABILITY_SOURCES = ("xg_blend", "market_spread")
UNKNOWN_SOURCE = "unknown"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _read_jsonl(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Read a JSONL file into a list of mappings; missing file means empty."""
    records: list[dict[str, Any]] = []
    if not Path(path).exists():
        return records
    with open(path, "r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            value = json.loads(line)
            if not isinstance(value, Mapping):
                raise ValueError(f"{path}:{line_no}: record must be a JSON object")
            records.append(dict(value))
    return records


def _write_jsonl_atomic(path: str | os.PathLike[str], records: Iterable[Mapping[str, Any]]) -> None:
    """Replace ``path`` atomically with serialized records."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".paper_bets-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def _append_jsonl(path: str | os.PathLike[str], records: Sequence[Mapping[str, Any]]) -> None:
    if not records:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _prob_text(value: Decimal) -> str:
    """10-decimal fixed-point text, never scientific notation."""
    return format(_decimal(value).quantize(PROBABILITY_PLACES, rounding=ROUND_HALF_UP), "f")


def _money_text(value: Decimal) -> str:
    return format(_decimal(value).quantize(MONEY_PLACES, rounding=ROUND_HALF_UP), "f")


def _amount4_text(value: Decimal) -> str:
    """Four decimal places for sd/se, where cents would hide small values."""
    return format(_decimal(value).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP), "f")


def _threshold_label(threshold: float | str | Decimal) -> str:
    value = _decimal(threshold).normalize()
    text = format(value, "f")
    return text if "." in text else text + ".0"


def _strategy_name(threshold: float | str | Decimal) -> str:
    return f"edge_gt_{_threshold_label(threshold)}"


def _bet_key(bet: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(bet.get("content_hash", "")),
        str(bet.get("label", "")),
        str(bet.get("strategy", "")),
    )


def _parse_kickoff(value: Any) -> tuple[int, str]:
    """Sort key for ISO timestamps; unparsable values sort last, then as text."""
    text = "" if value is None else str(value)
    try:
        return (0, datetime.fromisoformat(text).isoformat())
    except ValueError:
        return (1, text)


def _probability_source(record: Mapping[str, Any]) -> str:
    """``xg_blend`` when the prediction carries blended model outcomes.

    Otherwise the frozen probabilities came from the de-vigged European
    prices, i.e. a cross-book spread with no model input.
    """
    model = record.get("model")
    if isinstance(model, Mapping) and model.get("blended_outcomes"):
        return "xg_blend"
    return "market_spread"


def _prediction_sources(ledger_path: str | os.PathLike[str]) -> dict[str, str]:
    """Map prediction ``content_hash`` -> probability source.

    Used to backfill historical bets and as a fallback when a bet record
    predates the ``probability_source`` field.
    """
    sources: dict[str, str] = {}
    for record in _read_jsonl(ledger_path):
        if record.get("record_type") != "prediction":
            continue
        content_hash = record.get("content_hash")
        if not content_hash:
            continue
        source = _probability_source(record)
        # One hash backed by an xG blend must never be downgraded.
        if sources.get(str(content_hash)) != "xg_blend":
            sources[str(content_hash)] = source
    return sources


# ---------------------------------------------------------------------------
# 1. Bet selection -- prediction records only (information boundary)
# ---------------------------------------------------------------------------


def _selection_records(ledger_path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Return *only* frozen SPF predictions, projected onto SELECTION_FIELDS.

    Settlement records are deliberately filtered out here. Do not add any
    settlement-derived field to this projection.
    """
    selected: list[dict[str, Any]] = []
    for record in _read_jsonl(ledger_path):
        # Structural filters only; no settlement record is ever read below.
        if record.get("record_type") != "prediction":
            continue
        if record.get("status") != "frozen":
            continue
        if record.get("market") != "SPF":
            continue
        selected.append({field: record.get(field) for field in SELECTION_FIELDS})
    return selected


def _match_identity(record: Mapping[str, Any]) -> tuple[str, str] | None:
    """(match_id, kickoff_at) for one prediction, or ``None`` when unusable.

    The ledger legitimately contains two frozen ``primary`` rows for the same
    fixture (same ``match_id`` and ``kickoff_at``) whose ``content_hash``
    differs only because ``captured_at`` and the European odds capture were
    re-taken. A paper bet must not place the same match twice, so bet
    generation collapses them by this identity.
    """
    match_id = record.get("match_id")
    kickoff_at = record.get("kickoff_at")
    if not match_id or not kickoff_at:
        return None
    return (str(match_id), str(kickoff_at))


def _dedupe_by_match(
    records: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Keep one prediction per ``(match_id, kickoff_at)``, lowest hash first.

    Ordering by ``content_hash`` makes the survivor deterministic and
    reproducible. Records without a usable match identity are passed through
    unchanged rather than being collapsed together.
    """
    ordered = sorted(records, key=lambda record: str(record.get("content_hash") or ""))
    seen: set[tuple[str, str]] = set()
    kept: list[dict[str, Any]] = []
    for record in ordered:
        identity = _match_identity(record)
        if identity is None:
            kept.append(dict(record))
            continue
        if identity in seen:
            continue
        seen.add(identity)
        kept.append(dict(record))
    return kept


def _select_bet(
    record: Mapping[str, Any], threshold: Decimal, stake: float
) -> tuple[dict[str, Any] | None, str | None]:
    """Pick at most one outcome for one prediction; return (bet, skip_reason)."""
    content_hash = record.get("content_hash")
    if not content_hash:
        return None, "missing_content_hash"

    outcomes = record.get("outcomes")
    prizes = record.get("fixed_prizes")
    if not isinstance(outcomes, list) or not isinstance(prizes, Mapping):
        return None, "missing_fixed_prizes"

    probabilities: dict[str, Decimal] = {}
    for outcome in outcomes:
        if not isinstance(outcome, Mapping):
            return None, "malformed_outcomes"
        label = outcome.get("label")
        if label not in DEFAULT_OUTCOMES:
            continue
        try:
            probabilities[str(label)] = _decimal(outcome.get("probability"))
        except Exception:
            return None, "malformed_probability"
    if set(probabilities) != set(DEFAULT_OUTCOMES):
        return None, "incomplete_outcomes"

    odds: dict[str, str] = {}
    inverses: dict[str, Decimal] = {}
    for label in DEFAULT_OUTCOMES:
        raw = prizes.get(label)
        try:
            price = _decimal(raw)
        except Exception:
            return None, "malformed_prize"
        if price <= 0:
            return None, "non_positive_prize"
        odds[label] = str(raw)
        inverses[label] = Decimal(1) / price

    psum = sum(probabilities.values(), Decimal(0))
    inv_sum = sum(inverses.values(), Decimal(0))
    if psum <= 0 or inv_sum <= 0:
        return None, "non_positive_probability_sum"

    model_prob = {label: probabilities[label] / psum for label in DEFAULT_OUTCOMES}
    implied_prob = {label: inverses[label] / inv_sum for label in DEFAULT_OUTCOMES}
    edge = {label: model_prob[label] - implied_prob[label] for label in DEFAULT_OUTCOMES}

    best_label = max(DEFAULT_OUTCOMES, key=lambda label: edge[label])
    if edge[best_label] <= threshold:
        return None, "edge_not_above_threshold"

    context = record.get("match_context") or {}
    if not isinstance(context, Mapping):
        context = {}
    strategy = _strategy_name(threshold)
    bet = {
        "bet_id": f"{str(content_hash)[:16]}_{best_label}",
        "content_hash": content_hash,
        "match_id": record.get("match_id"),
        "kickoff_at": record.get("kickoff_at"),
        "home_team": context.get("home_team"),
        "away_team": context.get("away_team"),
        "competition": context.get("competition"),
        "label": best_label,
        "odds": odds[best_label],
        "stake": stake,
        "model_probability": _prob_text(model_prob[best_label]),
        "implied_probability": _prob_text(implied_prob[best_label]),
        "edge": _prob_text(edge[best_label]),
        "threshold": float(threshold),
        "strategy": strategy,
        "probability_source": _probability_source(record),
    }
    return bet, None


def generate_bets(
    ledger_path: str | os.PathLike[str] = DEFAULT_LEDGER,
    out_path: str | os.PathLike[str] = DEFAULT_OUT,
    threshold: float = DEFAULT_THRESHOLD,
    stake: float = 10.0,
) -> list[dict[str, Any]]:
    """Append new paper bets whose best model/implied edge exceeds ``threshold``.

    Two independent de-duplication layers apply:

    1. Match level: only the lowest-``content_hash`` prediction per
       ``(match_id, kickoff_at)`` may produce a bet, so one fixture never
       yields two paper bets.
    2. Idempotency: a bet already present in ``out_path`` with the same
       ``(content_hash, label, strategy)`` is skipped, so repeated runs never
       double-count.

    Returns only the records appended by this call.
    """
    if stake <= 0:
        raise ValueError("stake must be positive")
    threshold_dec = _decimal(threshold)

    existing = {_bet_key(record) for record in _read_jsonl(out_path)}
    new_bets: list[dict[str, Any]] = []
    # boundary: predictions only, then one record per match.
    for record in _dedupe_by_match(_selection_records(ledger_path)):
        bet, _reason = _select_bet(record, threshold_dec, stake)
        if bet is None:
            continue
        key = _bet_key(bet)
        if key in existing:
            continue
        existing.add(key)
        new_bets.append(bet)

    _append_jsonl(out_path, new_bets)
    return new_bets


# ---------------------------------------------------------------------------
# 2. Settlement -- settlement records are first read here (P&L boundary)
# ---------------------------------------------------------------------------


def _settlement_map(ledger_path: str | os.PathLike[str]) -> dict[str, str]:
    """Map prediction_hash -> actual_outcome, ignoring invalidated settlements.

    This is the first place settlement information is read. It must not be
    called from ``generate_bets``. An invalidated settlement no longer affects
    the tracker's reports, so it must not produce a paper P&L either.
    """
    settlements: dict[str, dict[str, Any]] = {}
    invalidated: set[str] = set()
    for record in _read_jsonl(ledger_path):
        record_type = record.get("record_type")
        if record_type == "settlement":
            settlement_hash = record.get("content_hash")
            if settlement_hash:
                settlements[str(settlement_hash)] = record
        elif record_type == "settlement_invalidation":
            settlement_hash = record.get("settlement_hash")
            if settlement_hash:
                invalidated.add(str(settlement_hash))

    outcome_by_prediction: dict[str, str] = {}
    for settlement_hash, record in settlements.items():
        if settlement_hash in invalidated:
            continue
        prediction_hash = record.get("prediction_hash")
        outcome = record.get("actual_outcome")
        if not prediction_hash or outcome is None:
            continue
        outcome_by_prediction[str(prediction_hash)] = str(outcome)
    return outcome_by_prediction


def settle_bets(
    ledger_path: str | os.PathLike[str] = DEFAULT_LEDGER,
    out_path: str | os.PathLike[str] = DEFAULT_OUT,
) -> int:
    """Fill P&L for settled paper bets; leave unsettled bets pending.

    Returns the number of bets updated. Existing records are never rewritten
    with a different settlement; a bet that already has ``settled_at`` is
    skipped.
    """
    bets = _read_jsonl(out_path)
    if not bets:
        return 0
    outcomes = _settlement_map(ledger_path)  # boundary: settlement stage only

    settled_at = datetime.now(timezone.utc).isoformat()
    updated = 0
    for bet in bets:
        if bet.get("settled_at"):
            continue
        prediction_hash = bet.get("content_hash")
        if prediction_hash is None or str(prediction_hash) not in outcomes:
            continue

        actual_outcome = outcomes[str(prediction_hash)]
        won = actual_outcome == bet.get("label")
        stake = _decimal(bet["stake"])
        if won:
            pnl = stake * (_decimal(bet["odds"]) - Decimal(1))
        else:
            pnl = -stake

        bet["settled_at"] = settled_at
        bet["actual_outcome"] = actual_outcome
        bet["pnl"] = _money_text(pnl)
        bet["won"] = won
        updated += 1

    if updated:
        _write_jsonl_atomic(out_path, bets)
    return updated


# ---------------------------------------------------------------------------
# 3. Report
# ---------------------------------------------------------------------------


def _is_settled(bet: Mapping[str, Any]) -> bool:
    return bool(bet.get("settled_at")) and bet.get("pnl") is not None


def _ratio(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    if denominator == 0:
        return None
    return numerator / denominator


def _pct(value: Decimal | None) -> str:
    if value is None:
        return "N/A"
    return f"{format(value * 100, '.2f')}%"


def _max_drawdown(bets: Sequence[Mapping[str, Any]]) -> Decimal:
    ordered = sorted(bets, key=lambda bet: _parse_kickoff(bet.get("kickoff_at")))
    equity = Decimal(0)
    peak = Decimal(0)
    worst = Decimal(0)
    for bet in ordered:
        equity += _decimal(bet["pnl"])
        if equity > peak:
            peak = equity
        drop = peak - equity
        if drop > worst:
            worst = drop
    return worst


def _strategy_stake(bets: Sequence[Mapping[str, Any]]) -> float:
    """Per-bet stake for one strategy; bets of a strategy share the same stake."""
    for bet in bets:
        raw = bet.get("stake")
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 10.0


def _strategy_stats(strategy: str, bets: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    stake = _strategy_stake(bets)
    settled = [bet for bet in bets if _is_settled(bet)]
    wins = [bet for bet in settled if bet.get("won") is True]

    invested = sum((_decimal(bet["stake"]) for bet in settled), Decimal(0))
    returned = sum(
        (_decimal(bet["stake"]) * _decimal(bet["odds"]) for bet in wins), Decimal(0)
    )
    net = returned - invested
    roi = _ratio(net, invested)
    hit_rate = _ratio(Decimal(len(wins)), Decimal(len(settled)))

    per_bet = [_decimal(bet["pnl"]) for bet in settled]
    sample_sd: Decimal | None = None
    standard_error: Decimal | None = None
    t_stat: Decimal | None = None
    n_need: int | None = None
    if len(per_bet) >= 2:
        mean = sum(per_bet, Decimal(0)) / Decimal(len(per_bet))
        variance = sum(((value - mean) ** 2 for value in per_bet), Decimal(0)) / Decimal(
            len(per_bet) - 1
        )
        sample_sd = variance.sqrt()
        standard_error = sample_sd / Decimal(len(per_bet)).sqrt()
        if standard_error > 0:
            t_stat = mean / standard_error
        # sd is in yuan per bet, so a +5% ROI (0.05 of stake) is 0.05*stake yuan.
        n_need = math.ceil((float(sample_sd) / (0.05 * stake)) ** 2 * 4)

    monthly: dict[str, dict[str, Decimal | int]] = {}
    for bet in settled:
        month = str(bet.get("kickoff_at") or "")[:7] or "unknown"
        bucket = monthly.setdefault(
            month,
            {"count": 0, "invested": Decimal(0), "returned": Decimal(0)},
        )
        bucket["count"] = int(bucket["count"]) + 1
        bucket["invested"] = _decimal(bucket["invested"]) + _decimal(bet["stake"])
        if bet.get("won") is True:
            bucket["returned"] = _decimal(bucket["returned"]) + (
                _decimal(bet["stake"]) * _decimal(bet["odds"])
            )

    return {
        "strategy": strategy,
        "total_bets": len(bets),
        "settled_bets": len(settled),
        "pending_bets": len(bets) - len(settled),
        "invested": invested,
        "returned": returned,
        "net": net,
        "roi": roi,
        "hit_rate": hit_rate,
        "sample_sd": sample_sd,
        "standard_error": standard_error,
        "t_stat": t_stat,
        "n_need": n_need,
        "stake": stake,
        "max_drawdown": _max_drawdown(settled),
        "monthly": monthly,
    }


def _ordered_strategies(
    by_strategy: Mapping[str, Sequence[Mapping[str, Any]]], current_strategy: str
) -> list[str]:
    """Current strategy first, then the remaining strategies by name."""
    ordered = [current_strategy] if current_strategy in by_strategy else []
    ordered.extend(sorted(name for name in by_strategy if name != current_strategy))
    return ordered


def _strategy_heading(strategy: str, current_strategy: str) -> str:
    if strategy == current_strategy:
        return f"[当前策略 {strategy}]"
    return f"[历史对照 {strategy} — 已停用，仅作对比]"


def report(
    ledger_path: str | os.PathLike[str] = DEFAULT_LEDGER,
    out_path: str | os.PathLike[str] = DEFAULT_OUT,
    current_threshold: float = DEFAULT_THRESHOLD,
) -> list[dict[str, Any]]:
    """Print per-strategy paper P&L statistics to stdout and return them.

    ``ledger_path`` is read only through ``_selection_records`` to report how
    many frozen SPF predictions collapse into unique matches. Stored P&L comes
    from ``out_path``; settlements were already applied by ``settle_bets``.

    ``current_threshold`` names the strategy produced by this run. It is
    printed first and labelled as current; every other strategy in the file is
    historical evidence and is printed afterwards as a comparison only.
    """
    bets = _read_jsonl(out_path)
    selection = _selection_records(ledger_path)  # boundary: predictions only
    unique_matches = len(_dedupe_by_match(selection))

    by_strategy: dict[str, list[Mapping[str, Any]]] = {}
    for bet in bets:
        by_strategy.setdefault(str(bet.get("strategy", "unknown")), []).append(bet)

    current_strategy = _strategy_name(current_threshold)
    ordered = _ordered_strategies(by_strategy, current_strategy)

    print("== 纸面注单盈亏报告（paper-only，不构成任何购买建议）==")
    print(f"注单文件: {out_path}")
    print(
        f"预测记录 {len(selection)} 条 → 唯一比赛 {unique_matches} 场"
        f"（去重 {len(selection) - unique_matches} 条同日重复）"
    )
    print(f"注单总数: {len(bets)}")
    source_totals: dict[str, int] = {}
    for bet in bets:
        key = str(bet.get("probability_source") or "unknown")
        source_totals[key] = source_totals.get(key, 0) + 1
    if source_totals:
        parts = " / ".join(f"{name} {count} 注" for name, count in sorted(source_totals.items()))
        print(f"概率来源: {parts}")
    if current_strategy not in by_strategy:
        print(f"当前策略 {current_strategy}: 暂无注单")
    print()

    all_stats: list[dict[str, Any]] = []
    for strategy in ordered:
        stats = _strategy_stats(strategy, by_strategy[strategy])
        all_stats.append(stats)
        _print_stats(stats, _strategy_heading(strategy, current_strategy))
        _print_source_breakdown(by_strategy[strategy])
        print()

    _print_comparison(all_stats, current_strategy)
    return all_stats


def _print_comparison(
    all_stats: Sequence[Mapping[str, Any]], current_strategy: str
) -> None:
    """Side-by-side view only: the strategies place separate bets on the same
    matches, so their records must never be summed into one ROI."""
    print("[策略对比（各自独立下注，不可相加）]")
    if not all_stats:
        print("  无注单")
        return
    for stats in all_stats:
        t_text = "N/A" if stats["t_stat"] is None else format(stats["t_stat"], ".4f")
        tag = "当前策略" if stats["strategy"] == current_strategy else "历史对照"
        print(
            f"  {stats['strategy']}（{tag}）: "
            f"注数 {stats['settled_bets']} "
            f"ROI {_pct(stats['roi'])} "
            f"t {t_text} "
            f"最大回撤 {_money_text(stats['max_drawdown'])}"
        )


def _print_stats(stats: Mapping[str, Any], heading: str) -> None:
    print(heading)
    print(f"  注数: 总 {stats['total_bets']} / 已结算 {stats['settled_bets']} / 待结算 {stats['pending_bets']}")
    print(f"  总投入: {_money_text(stats['invested'])}")
    print(f"  总返还: {_money_text(stats['returned'])}")
    print(f"  净收益: {_money_text(stats['net'])}")
    print(f"  ROI: {_pct(stats['roi'])}")
    print(f"  命中率: {_pct(stats['hit_rate'])}")

    if stats["sample_sd"] is None:
        print("  每注收益: 已结算样本不足 2 注，sd/se/t 不可用")
        print("  +5% ROI 所需注数: N/A")
    else:
        t_text = "N/A" if stats["t_stat"] is None else format(stats["t_stat"], ".4f")
        print(
            "  每注收益: "
            f"sd={_amount4_text(stats['sample_sd'])} "
            f"se={_amount4_text(stats['standard_error'])} "
            f"t={t_text} (n={stats['settled_bets']})"
        )
        print(
            f"  +5% ROI 所需注数: ceil((sd/(0.05*stake))^2*4) = {stats['n_need']} 注"
        )
    print(f"  最大回撤: {_money_text(stats['max_drawdown'])}")

    monthly = stats["monthly"]
    if not monthly:
        print("  按月 ROI: 无已结算注单")
        return
    print("  按月 ROI:")
    for month in sorted(monthly):
        bucket = monthly[month]
        invested = _decimal(bucket["invested"])
        returned = _decimal(bucket["returned"])
        roi = _ratio(returned - invested, invested)
        print(
            f"    {month}: 注数 {bucket['count']} 投入 {_money_text(invested)} "
            f"返还 {_money_text(returned)} ROI {_pct(roi)}"
        )


SOURCE_LABELS = {"xg_blend": "xg_blend", "market_spread": "market_spread"}
SOURCE_NOTES = {
    "xg_blend": "0.7×欧赔 + 0.3×xG泊松，真模型参与",
    "market_spread": "纯欧赔去水 vs 体彩，跨庄价差，模型未参与",
}


def _print_source_breakdown(bets: Sequence[Mapping[str, Any]]) -> None:
    """Split one strategy by where its probabilities came from.

    The two sources are not comparable: ``xg_blend`` carries model input while
    ``market_spread`` is a cross-bookmaker price gap with no model involved.
    Mixing them hides which one actually earns, so always show them apart.
    """
    by_source: dict[str, list[Mapping[str, Any]]] = {}
    for bet in bets:
        source = str(bet.get("probability_source") or "unknown")
        by_source.setdefault(source, []).append(bet)

    if len(by_source) < 2:
        return

    print("  按概率来源拆分:")
    for source in sorted(by_source):
        stats = _strategy_stats(source, by_source[source])
        t_text = "N/A" if stats["t_stat"] is None else format(stats["t_stat"], ".2f")
        note = SOURCE_NOTES.get(source, "来源未知")
        print(
            f"    {SOURCE_LABELS.get(source, source):<14}"
            f"n={stats['settled_bets']:<4}"
            f"净{_money_text(stats['net']):>9} "
            f"ROI{_pct(stats['roi']):>9} "
            f"命中{_pct(stats['hit_rate']):>7} "
            f"t={t_text}   {note}"
        )


def _backfill_probability_source(
    out_path: str | os.PathLike[str], ledger_path: str | os.PathLike[str]
) -> int:
    """Stamp ``probability_source`` onto bets written before the field existed.

    Bets are keyed by content_hash, so the frozen prediction behind each bet
    can be looked up directly. Idempotent: only records missing the field are
    rewritten, and the write is skipped entirely when nothing is missing.
    """
    bets = _read_jsonl(out_path)
    if not bets or all(bet.get("probability_source") for bet in bets):
        return 0

    records = _read_jsonl(ledger_path)
    by_hash: dict[str, Mapping[str, Any]] = {}
    for record in records:
        if record.get("record_type") == "prediction":
            digest = record.get("content_hash")
            if isinstance(digest, str):
                by_hash[digest] = record

    filled = 0
    updated: list[dict[str, Any]] = []
    for bet in bets:
        item = dict(bet)
        if not item.get("probability_source"):
            prediction = by_hash.get(str(item.get("content_hash")))
            if prediction is not None:
                item["probability_source"] = _probability_source(prediction)
                filled += 1
        updated.append(item)

    if filled:
        _write_jsonl_atomic(out_path, updated)
    return filled


# ---------------------------------------------------------------------------
# 4. CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m outputs.tai_cai_tracker.paper_bet",
        description="纸面注单记账与盈亏报告（只读台账，不写 paper_ledger.jsonl）",
    )
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER), help="只读台账路径")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="注单输出路径")
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help="edge 阈值，默认 0.05（实测 0~5 点为噪声，5 点以上才下注）",
    )
    parser.add_argument("--stake", type=float, default=10.0, help="每注纸面本金")
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="只输出报告，不生成或结算注单",
    )
    parser.add_argument(
        "--no-report",
        action="store_true",
        help="只生成/结算注单，不打印盈亏报告",
    )
    return parser


def _summary_line(
    strategy: str,
    out_path: str | os.PathLike[str],
    n_new: int,
    n_settle: int,
) -> str:
    """One-line summary for scheduler logs; counts are recomputed on read."""
    current = [bet for bet in _read_jsonl(out_path) if str(bet.get("strategy")) == strategy]
    n_settled = sum(1 for bet in current if _is_settled(bet))
    return (
        f"纸面投注: 新增 {n_new} 注, 结算 {n_settle} 注, "
        f"当前策略 {strategy} 累计 {len(current)} 注 (已结算 {n_settled})"
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    new_bets: list[dict[str, Any]] = []
    updated = 0
    if not args.report_only:
        filled = _backfill_probability_source(args.out, args.ledger)
        if filled and not args.no_report:
            print(f"补齐概率来源字段: {filled} 条旧注单")
        new_bets = generate_bets(
            args.ledger, args.out, threshold=args.threshold, stake=args.stake
        )
        strategy = _strategy_name(args.threshold)
        if not args.no_report:
            print(f"生成注单[{strategy}]: 新增 {len(new_bets)} 注（幂等，已存在则跳过）")
        updated = settle_bets(args.ledger, args.out)
        if not args.no_report:
            print(f"结算注单: 更新 {updated} 注")
            print()

    if args.no_report:
        print(
            _summary_line(
                _strategy_name(args.threshold), args.out, len(new_bets), updated
            )
        )
        return 0

    report(args.ledger, args.out, current_threshold=args.threshold)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
