#!/usr/bin/env python3
"""Read-only comparison of two market-probability sources against settled results.

A: China Sports Lottery fixed prizes on frozen prediction records.
B: Independent European 1X2 odds captures (de-vigged), matched by
   (match_id, kickoff_at) and restricted to captures at or before the
   prediction's captured_at.

The ledger is only read. Nothing is written anywhere.
"""

from __future__ import annotations

import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from outputs.tai_cai_tracker.model import normalize_fixed_prizes  # noqa: E402

LEDGER = ROOT / "outputs" / "tai_cai_tracker" / "data" / "paper_ledger.jsonl"
LABELS = ("home_win", "draw", "away_win")
LABEL_CN = {"home_win": "主胜", "draw": "平", "away_win": "客胜"}
CLAMP = 1e-12


def parse_time(value):
    """Parse an ISO timestamp into an aware datetime; return None if unusable."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def load_ledger():
    rows = []
    with LEDGER.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as error:
                raise SystemExit(f"ledger line {line_number}: {error}") from error
    return rows


def exact_normalize(weights):
    """Normalize Decimal weights so the three probabilities sum to exactly 1.

    Every component is rounded to a fixed 50 decimal places first, then the
    largest component absorbs the residual. Both operands of that subtraction
    share the same exponent, so the result is exact and the triple sums to one.
    """
    quantum = Decimal(1).scaleb(-50)
    with localcontext() as context:
        context.prec = 160
        total = sum(weights.values(), Decimal(0))
        probabilities = {
            label: (weights[label] / total).quantize(quantum, rounding=ROUND_HALF_EVEN)
            for label in LABELS
        }
        largest = max(LABELS, key=lambda label: probabilities[label])
        probabilities[largest] = Decimal(1).quantize(quantum) - sum(
            (probabilities[label] for label in LABELS if label != largest),
            Decimal(0),
        )
        # Check inside the high-precision context: summing at the default
        # 28-digit precision would itself round.
        assert sum(probabilities.values(), Decimal(0)) == Decimal(1)
    return probabilities


def prizes_to_probabilities(prizes):
    """A: reciprocal normalization of official fixed prizes."""
    if not isinstance(prizes, dict):
        return None
    try:
        values = [Decimal(str(prizes[label])) for label in LABELS]
    except (KeyError, ArithmeticError, ValueError):
        return None
    if any(not value.is_finite() or value <= 0 for value in values):
        return None
    normalized = normalize_fixed_prizes(values)
    probabilities = {label: normalized[index] for index, label in enumerate(LABELS)}
    if any(probabilities[label] <= 0 for label in LABELS):
        return None
    assert sum(probabilities.values(), Decimal(0)) == Decimal(1)
    return probabilities


def odds_to_probabilities(odds):
    """B: de-vig the three decimal European prices, then force an exact unit sum."""
    if not isinstance(odds, dict):
        return None
    try:
        prices = {label: Decimal(str(odds[label])) for label in LABELS}
    except (KeyError, ArithmeticError, ValueError):
        return None
    if any(not price.is_finite() or price <= 1 for price in prices.values()):
        return None
    with localcontext() as context:
        context.prec = 60
        reciprocals = {label: Decimal(1) / prices[label] for label in LABELS}
    return exact_normalize(reciprocals)


def settlement_outcome(settlement):
    """Return (label, score_text, used_final_score) for a settlement record."""
    score = settlement.get("final_score")
    if isinstance(score, dict) and "home" in score and "away" in score:
        try:
            home = int(score["home"])
            away = int(score["away"])
        except (TypeError, ValueError):
            home = away = None
        if home is not None and away is not None:
            if home > away:
                return "home_win", f"{home}-{away}", True
            if home < away:
                return "away_win", f"{home}-{away}", True
            return "draw", f"{home}-{away}", True
    fallback = settlement.get("actual_outcome")
    if fallback in LABELS:
        return fallback, "无 final_score", False
    return None, "无有效比分", False


def brier(probabilities, outcome):
    return sum(
        (float(probabilities[label]) - (1.0 if label == outcome else 0.0)) ** 2
        for label in LABELS
    )


def log_loss(probabilities, outcome):
    probability = min(max(float(probabilities[outcome]), CLAMP), 1.0)
    return -math.log(probability)


def summarize(samples, key):
    n = len(samples)
    if not n:
        return n, None, None
    return (
        n,
        sum(sample[f"brier_{key}"] for sample in samples) / n,
        sum(sample[f"logloss_{key}"] for sample in samples) / n,
    )


def fmt(value, digits=6):
    return "N/A" if value is None else f"{value:.{digits}f}"


def print_table(headers, rows, aligns):
    widths = [len(header) for header in headers]
    rendered = []
    for row in rows:
        cells = [str(cell) for cell in row]
        rendered.append(cells)
        for index, cell in enumerate(cells):
            widths[index] = max(widths[index], len(cell))
    print("  ".join(f"{header:^{widths[i]}}" for i, header in enumerate(headers)))
    print("  ".join("-" * width for width in widths))
    for cells in rendered:
        print(
            "  ".join(
                f"{cell:>{widths[i]}}" if aligns[i] == "r" else f"{cell:<{widths[i]}}"
                for i, cell in enumerate(cells)
            )
        )


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    rows = load_ledger()
    predictions = [row for row in rows if row.get("record_type") == "prediction"]
    settlements = [row for row in rows if row.get("record_type") == "settlement"]
    invalidations = [
        row for row in rows if row.get("record_type") == "settlement_invalidation"
    ]
    captures = [row for row in rows if row.get("record_type") == "odds_capture"]

    invalidated_hashes = {
        row.get("settlement_hash") for row in invalidations if row.get("settlement_hash")
    }

    # Valid settlement per prediction hash: drop invalidated records, keep the last
    # valid record if a prediction somehow still has more than one.
    settlements_by_prediction = {}
    dropped_invalidated = 0
    for settlement in settlements:
        if settlement.get("content_hash") in invalidated_hashes:
            dropped_invalidated += 1
            continue
        prediction_hash = settlement.get("prediction_hash")
        if prediction_hash:
            settlements_by_prediction[prediction_hash] = settlement

    # Captures usable for time-ordered lookup, keyed by (match_id, kickoff instant).
    captures_by_match = defaultdict(list)
    for capture in captures:
        kickoff = parse_time(capture.get("kickoff_at"))
        captured = parse_time(capture.get("captured_at"))
        if kickoff is None or captured is None or not capture.get("match_id"):
            continue
        captures_by_match[(capture["match_id"], kickoff)].append(
            {"capture": capture, "captured": captured}
        )
    for entries in captures_by_match.values():
        entries.sort(key=lambda entry: entry["captured"])

    samples = []
    stats = Counter()

    for prediction in predictions:
        stats["predictions"] += 1
        prediction_hash = prediction.get("content_hash")
        kickoff = parse_time(prediction.get("kickoff_at"))
        prediction_captured = parse_time(prediction.get("captured_at"))

        a_probabilities = prizes_to_probabilities(prediction.get("fixed_prizes"))
        if prediction.get("fixed_prizes") is None:
            stats["a_missing_prizes"] += 1
        elif a_probabilities is None:
            stats["a_invalid_prizes"] += 1

        b_probabilities = None
        latest_capture = None
        if kickoff is None:
            stats["b_bad_kickoff"] += 1
        elif prediction_captured is None:
            stats["b_bad_prediction_time"] += 1
        else:
            entries = captures_by_match.get((prediction.get("match_id"), kickoff), [])
            usable = [
                entry for entry in entries if entry["captured"] <= prediction_captured
            ]
            stats["b_capture_pairs_total"] += len(entries)
            stats["b_capture_pairs_usable"] += len(usable)
            if not entries:
                stats["b_no_capture_same_fixture"] += 1
            elif not usable:
                stats["b_only_after_prediction"] += 1
            else:
                latest_capture = usable[-1]["capture"]
                b_probabilities = odds_to_probabilities(latest_capture.get("odds"))
                if b_probabilities is None:
                    stats["b_invalid_odds"] += 1

        settlement = settlements_by_prediction.get(prediction_hash)
        outcome = None
        score_text = ""
        if settlement is None:
            stats["unsettled"] += 1
        else:
            outcome, score_text, used_final_score = settlement_outcome(settlement)
            if outcome is None:
                stats["unsettled"] += 1
            elif not used_final_score:
                stats["settlement_without_final_score"] += 1

        if a_probabilities is not None:
            stats["a_available"] += 1
        if b_probabilities is not None:
            stats["b_available"] += 1
        if a_probabilities is not None and b_probabilities is not None:
            stats["both_available"] += 1

        if outcome is None:
            continue

        context = prediction.get("match_context") or {}
        sample = {
            "match_id": prediction.get("match_id"),
            "kickoff_at": prediction.get("kickoff_at"),
            "prediction_hash": prediction_hash,
            "home_team": context.get("home_team"),
            "away_team": context.get("away_team"),
            "outcome": outcome,
            "score": score_text,
            "a": a_probabilities,
            "b": b_probabilities,
            "bookmaker": (latest_capture or {}).get("bookmaker") or "unattributed",
            "provider": (latest_capture or {}).get("provider") or "unattributed",
        }
        sample["brier_A"] = brier(a_probabilities, outcome) if a_probabilities else None
        sample["logloss_A"] = log_loss(a_probabilities, outcome) if a_probabilities else None
        sample["brier_B"] = brier(b_probabilities, outcome) if b_probabilities else None
        sample["logloss_B"] = log_loss(b_probabilities, outcome) if b_probabilities else None
        samples.append(sample)

    a_samples = [sample for sample in samples if sample["a"] is not None]
    b_samples = [sample for sample in samples if sample["b"] is not None]
    both_samples = [
        sample for sample in samples if sample["a"] is not None and sample["b"] is not None
    ]

    print("=" * 78)
    print("市场概率来源对比（只读）: A=体彩固定奖金, B=欧洲赔率去水")
    print("=" * 78)
    print(f"台账: {LEDGER}")
    print("关联键: (match_id, kickoff_at)；B 仅取 captured_at <= prediction.captured_at 的最新一条")
    print(f"被 settlement_invalidation 作废的 settlement 已排除: {dropped_invalidated} 条")
    print()

    print("-" * 78)
    print("1. 样本量")
    print("-" * 78)
    print(f"prediction 总数                        : {stats['predictions']}")
    print(f"A 可用（合法 fixed_prizes 且已结算）    : {len(a_samples)}")
    print(f"B 可用（赛前配对报价且已结算）          : {len(b_samples)}")
    print(f"A∩B 都可用（公平对比基准）              : {len(both_samples)}")
    print()
    print("排除明细:")
    print(f"  A: 无 fixed_prizes 字段                 : {stats['a_missing_prizes']}")
    print(f"  A: fixed_prizes 非法                    : {stats['a_invalid_prizes']}")
    print(f"  B: kickoff_at 无法解析                  : {stats['b_bad_kickoff']}")
    print(f"  B: prediction captured_at 无法解析      : {stats['b_bad_prediction_time']}")
    print(f"  B: 同场次无任何欧洲赔率记录              : {stats['b_no_capture_same_fixture']}")
    print(f"  B: 仅有晚于预测冻结时间的报价            : {stats['b_only_after_prediction']}")
    print(f"  B: 赔率记录非法                         : {stats['b_invalid_odds']}")
    print(f"  无有效结算（作废后无替代）              : {stats['unsettled']}")
    print(f"  仅有 actual_outcome 而无 final_score    : {stats['settlement_without_final_score']}")
    print(
        f"  B 候选报价: 共 {stats['b_capture_pairs_total']} 条，"
        f"满足 captured_at 约束 {stats['b_capture_pairs_usable']} 条"
    )
    print()

    print("-" * 78)
    print("2. Brier / LogLoss")
    print("-" * 78)
    n_a, brier_a, ll_a = summarize(a_samples, "A")
    n_b, brier_b, ll_b = summarize(b_samples, "B")
    n_x, brier_ax, ll_ax = summarize(both_samples, "A")
    _, brier_bx, ll_bx = summarize(both_samples, "B")
    print_table(
        ["来源", "样本数", "Brier", "LogLoss"],
        [
            ["A 体彩固定奖金（全部 A 可用）", n_a, fmt(brier_a), fmt(ll_a)],
            ["B 欧洲赔率去水（全部 B 可用）", n_b, fmt(brier_b), fmt(ll_b)],
            ["A 体彩固定奖金（仅 A∩B）", n_x, fmt(brier_ax), fmt(ll_ax)],
            ["B 欧洲赔率去水（仅 A∩B）", n_x, fmt(brier_bx), fmt(ll_bx)],
        ],
        ["l", "r", "r", "r"],
    )
    print()

    print("-" * 78)
    print("3. B 按 bookmaker 拆分（样本 < 3 合并为“其他”）")
    print("-" * 78)
    by_bookmaker = defaultdict(list)
    for sample in b_samples:
        by_bookmaker[sample["bookmaker"]].append(sample)
    bookmaker_rows = []
    other = []
    for name, members in by_bookmaker.items():
        if len(members) < 3:
            other.extend(members)
        else:
            bookmaker_rows.append(
                [name, len(members), fmt(sum(s["brier_B"] for s in members) / len(members))]
            )
    bookmaker_rows.sort(key=lambda row: (-row[1], row[0]))
    if other:
        bookmaker_rows.append(
            ["其他", len(other), fmt(sum(s["brier_B"] for s in other) / len(other))]
        )
    print_table(["bookmaker", "样本数", "Brier"], bookmaker_rows, ["l", "r", "r"])
    print()

    print("-" * 78)
    print("4. B 按 provider 拆分（按样本数前 6）")
    print("-" * 78)
    by_provider = defaultdict(list)
    for sample in b_samples:
        by_provider[sample["provider"]].append(sample)
    provider_rows = [
        [name, len(members), fmt(sum(s["brier_B"] for s in members) / len(members))]
        for name, members in by_provider.items()
    ]
    provider_rows.sort(key=lambda row: (-row[1], row[0]))
    print_table(["provider", "样本数", "Brier"], provider_rows[:6], ["l", "r", "r"])
    print()

    print("-" * 78)
    print("5. 两来源主胜概率分歧最大的 5 场")
    print("-" * 78)
    divergent = sorted(
        both_samples,
        key=lambda sample: -abs(float(sample["a"]["home_win"]) - float(sample["b"]["home_win"])),
    )[:5]
    for rank, sample in enumerate(divergent, 1):
        home = sample["home_team"] or "?"
        away = sample["away_team"] or "?"
        print(
            f"[{rank}] {sample['match_id']}  {home} vs {away}  "
            f"比分 {sample['score']} ({LABEL_CN[sample['outcome']]})"
        )
        print(f"    开球 {sample['kickoff_at']}  bookmaker={sample['bookmaker']}")
        print(
            "    A: "
            + " / ".join(
                f"{LABEL_CN[label]} {float(sample['a'][label]):.4f}" for label in LABELS
            )
            + f"   Brier={sample['brier_A']:.6f}"
        )
        print(
            "    B: "
            + " / ".join(
                f"{LABEL_CN[label]} {float(sample['b'][label]):.4f}" for label in LABELS
            )
            + f"   Brier={sample['brier_B']:.6f}"
        )
        print(
            "    |Δp_home| = "
            f"{abs(float(sample['a']['home_win']) - float(sample['b']['home_win'])):.4f}"
        )
    print()
    print("注：同一 (match_id, kickoff_at) 存在重复 prediction 记录时，本表按记录数计样本。")


if __name__ == "__main__":
    main()
