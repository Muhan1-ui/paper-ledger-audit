#!/usr/bin/env python
"""Offline Brier / LogLoss evaluation of blending FotMob xG into 1X2 predictions.

Read-only: this script only reads the ledger and the FotMob backfill file.
It never writes, mutates, or appends to either file.
"""

from __future__ import annotations

import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal, localcontext
from pathlib import Path

# The Windows console defaults to a legacy code page; force UTF-8 so Chinese
# team / competition names are readable when stdout is captured.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from outputs.tai_cai_tracker.model import (  # noqa: E402
    OUTCOME_LABELS,
    blend_probabilities,
    normalize_fixed_prizes,
    poisson_score_matrix,
)

LEDGER_PATH = ROOT / "outputs/tai_cai_tracker/data/paper_ledger.jsonl"
BACKFILL_PATH = ROOT / "outputs/tai_cai_tracker/data/fotmob_xg_backfill.jsonl"

LABELS = ("home_win", "draw", "away_win")
LABEL_CN = {"home_win": "主胜", "draw": "平", "away_win": "客胜"}
WEIGHTS = [Decimal("0.0"), Decimal("0.1"), Decimal("0.2"),
           Decimal("0.3"), Decimal("0.4"), Decimal("0.5")]
EVAL_WEIGHT = Decimal("0.3")


def load_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def settle_outcome(final_score: dict) -> str:
    home = int(final_score["home"])
    away = int(final_score["away"])
    if home > away:
        return "home_win"
    if home < away:
        return "away_win"
    return "draw"


def as_decimal(value) -> Decimal:
    return Decimal(str(value))


def decimal_dict(raw: dict[str, str]) -> dict[str, Decimal]:
    return {label: Decimal(raw[label]) for label in LABELS}


def _normalise_exact(values: list[Decimal]) -> list[Decimal]:
    """Scale Decimals so they sum to exactly Decimal(1).

    The Poisson matrix is truncated at max_goals, and Decimal division leaves a
    tiny residual, so poisson's one_x_two can miss an exact unit sum by one
    final-digit unit.  blend_probabilities requires strict equality, so the
    leftover is folded into the largest label.
    """
    total = sum(values, Decimal(0))
    if total == 0:
        return list(values)
    with localcontext() as context:
        context.prec = max(context.prec, 60)
        scaled = [value / total for value in values]
        diff = Decimal(1) - sum(scaled, Decimal(0))
        if diff:
            index = max(range(len(scaled)), key=lambda j: scaled[j])
            scaled[index] += diff
        if sum(scaled, Decimal(0)) != Decimal(1):
            # Adding the leftover can itself round; pin the largest label to
            # the exact complement of the other two instead.
            index = max(range(len(scaled)), key=lambda j: scaled[j])
            scaled[index] = Decimal(1) - sum(
                (value for j, value in enumerate(scaled) if j != index),
                Decimal(0),
            )
    return scaled


def _normalised_dict(distribution: dict[str, Decimal]) -> dict[str, Decimal]:
    values = _normalise_exact([distribution[label] for label in OUTCOME_LABELS])
    return dict(zip(OUTCOME_LABELS, values))


def brier(probs: dict[str, Decimal], actual: str) -> float:
    total = Decimal(0)
    for label in LABELS:
        target = Decimal(1) if label == actual else Decimal(0)
        diff = probs[label] - target
        total += diff * diff
    return float(total)


def log_loss(probs: dict[str, Decimal], actual: str) -> float:
    probability = float(probs[actual])
    probability = min(1.0, max(1e-12, probability))
    return -math.log(probability)


def fmt_probs(probs: dict[str, Decimal]) -> str:
    return "/".join(f"{float(probs[label]):.4f}" for label in LABELS)


def kickoff(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def main() -> None:
    records = load_jsonl(LEDGER_PATH)
    backfill = load_jsonl(BACKFILL_PATH)

    # ---- invalidated settlements -------------------------------------------------
    invalidated_settlement_hashes = {
        record["settlement_hash"]
        for record in records
        if record.get("record_type") == "settlement_invalidation"
        and record.get("settlement_hash")
    }

    settlements_by_prediction: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        if record.get("record_type") == "settlement" and record.get("prediction_hash"):
            settlements_by_prediction[record["prediction_hash"]].append(record)

    # ---- primary predictions, deduplicated by actual match ----------------------
    # match_id alone repeats across weeks.  (match_id, home_team) is the required
    # join key, but it can still repeat when the same team lands in the same
    # weekday slot in different rounds, and the ledger contains a handful of
    # byte-for-byte duplicate freezes.  kickoff_at is used only as a tie-breaker
    # so those cases are not counted twice or given another week's xG.
    raw_primaries = [
        record
        for record in records
        if record.get("record_type") == "prediction"
        and record.get("prediction_role") == "primary"
    ]
    deduped: dict[tuple, dict] = {}
    duplicate_groups = 0
    for record in raw_primaries:
        context = record.get("match_context") or {}
        key = (record.get("match_id"), context.get("home_team"), record.get("kickoff_at"))
        candidate = deduped.get(key)
        if candidate is None:
            deduped[key] = record
            continue
        duplicate_groups += 1
        if (record.get("captured_at") or "") > (candidate.get("captured_at") or ""):
            deduped[key] = record
    primaries = list(deduped.values())

    # ---- backfill index: (match_id, home_team) ---------------------------------
    backfill_by_key: dict[tuple, list[dict]] = defaultdict(list)
    for row in backfill:
        backfill_by_key[(row.get("ledger_match_id"), row.get("home_team"))].append(row)

    def backfill_for(prediction: dict) -> dict | None:
        context = prediction.get("match_context") or {}
        candidates = backfill_by_key.get(
            (prediction.get("match_id"), context.get("home_team")), []
        )
        if not candidates:
            return None
        if len(candidates) == 1:
            only = candidates[0]
            pred_kickoff = kickoff(prediction.get("kickoff_at"))
            bf_kickoff = kickoff(only.get("kickoff_at"))
            # Reject an xG row from a different round that happens to share the
            # same weekday slot and home team.
            if pred_kickoff and bf_kickoff:
                if abs((pred_kickoff - bf_kickoff).total_seconds()) > 36 * 3600:
                    return None
            return only
        pred_kickoff = kickoff(prediction.get("kickoff_at"))
        ranked = sorted(
            candidates,
            key=lambda row: abs(
                (pred_kickoff - kickoff(row.get("kickoff_at"))).total_seconds()
            )
            if pred_kickoff and kickoff(row.get("kickoff_at"))
            else float("inf"),
        )
        return ranked[0]

    # ---- assemble evaluable matches --------------------------------------------
    xg_matches: list[dict] = []
    own_xg_count = 0
    backfill_xg_count = 0
    invalidated_linked = 0
    no_valid_settlement = 0
    missing_final_score = 0

    for prediction in primaries:
        context = prediction.get("match_context") or {}
        own_inputs = prediction.get("xg_inputs") or {}
        source = None
        home_xg = away_xg = None
        if own_inputs.get("home_xg") is not None and own_inputs.get("away_xg") is not None:
            source = "ledger_xg_inputs"
            home_xg = as_decimal(own_inputs["home_xg"])
            away_xg = as_decimal(own_inputs["away_xg"])
            own_xg_count += 1
        else:
            row = backfill_for(prediction)
            xg = (row or {}).get("xg") or {}
            if xg.get("home_xg") is not None and xg.get("away_xg") is not None:
                source = "fotmob_backfill"
                home_xg = as_decimal(xg["home_xg"])
                away_xg = as_decimal(xg["away_xg"])
                backfill_xg_count += 1
        if source is None:
            continue

        settlements = settlements_by_prediction.get(prediction.get("content_hash"), [])
        valid = [s for s in settlements if s.get("content_hash") not in invalidated_settlement_hashes]
        invalidated = [s for s in settlements if s.get("content_hash") in invalidated_settlement_hashes]
        if invalidated:
            invalidated_linked += 1
        if not valid:
            no_valid_settlement += 1
            continue
        usable = [s for s in valid if s.get("final_score")]
        if not usable:
            missing_final_score += 1
            continue
        # Append-only ledger: the last valid settlement is the operative one.
        settlement = usable[-1]

        raw_outcomes = {item["label"]: item["probability"] for item in prediction.get("outcomes", [])}
        if set(raw_outcomes) == set(LABELS):
            market = decimal_dict(raw_outcomes)
        else:
            prizes = prediction.get("fixed_prizes") or {}
            market = dict(zip(
                LABELS,
                normalize_fixed_prizes([Decimal(prizes[label]) for label in LABELS]),
            ))

        xg_matches.append({
            "match_id": prediction.get("match_id"),
            "home_team": context.get("home_team") or "?",
            "away_team": context.get("away_team") or "?",
            "competition": context.get("competition") or "其他",
            "source": source,
            "home_xg": home_xg,
            "away_xg": away_xg,
            "market": market,
            "actual": settle_outcome(settlement["final_score"]),
            "final_score": f'{settlement["final_score"]["home"]}-{settlement["final_score"]["away"]}',
        })

    # ---- model probabilities and per-weight metrics ---------------------------
    for match in xg_matches:
        model = poisson_score_matrix(match["home_xg"], match["away_xg"])["one_x_two"]
        # Both sides must sum to exactly one before blend_probabilities sees them.
        model = _normalised_dict(model)
        match["market"] = _normalised_dict(match["market"])
        match["model"] = model
        match["blended"] = {}
        for weight in WEIGHTS:
            if weight == 0:
                match["blended"][weight] = match["market"]
            else:
                match["blended"][weight] = blend_probabilities(
                    match["market"], model, weight
                )
        match["brier"] = {
            weight: brier(match["blended"][weight], match["actual"]) for weight in WEIGHTS
        }
        match["logloss"] = {
            weight: log_loss(match["blended"][weight], match["actual"]) for weight in WEIGHTS
        }

    print("=" * 78)
    print("FotMob xG 融合离线评测（只读台账）")
    print("=" * 78)
    print("口径说明：回填 xG 取自赛后 FotMob 比赛数据，含赛后信息；")
    print("          台账 xg_inputs 为赛前 league_total 估计。两者混合评估仅作诊断。")
    print()

    # ---- 1. sample sizes ------------------------------------------------------
    print("===== 1. 样本量 =====")
    print(f"primary 预测总数: {len(raw_primaries)}（同场次重复冻结去重 {duplicate_groups} 条，去重后 {len(primaries)} 场）")
    print(f"取得 xG 的场次: {len(xg_matches) + no_valid_settlement + missing_final_score}")
    print(f"  来自台账 xg_inputs: {own_xg_count}")
    print(f"  来自 FotMob 回填: {backfill_xg_count}")
    print(f"其中结算被作废: {invalidated_linked}（作废结算不计入真实结果）")
    print(f"无有效结算而排除: {no_valid_settlement}")
    print(f"有效结算但缺 final_score 而排除: {missing_final_score}")
    print(f"最终评测样本: {len(xg_matches)}")
    print()

    if not xg_matches:
        print("没有可评测样本，结束。")
        return

    # ---- 2. weight table ------------------------------------------------------
    print("===== 2. 混合权重评测（升序）=====")
    print(f"{'w':<14}{'样本数':>8}{'Brier':>12}{'LogLoss':>12}")
    for weight in WEIGHTS:
        row_brier = sum(match["brier"][weight] for match in xg_matches) / len(xg_matches)
        row_logloss = sum(match["logloss"][weight] for match in xg_matches) / len(xg_matches)
        label = f"{float(weight):.1f}" + (" (baseline)" if weight == 0 else "")
        print(f"{label:<14}{len(xg_matches):>8}{row_brier:>12.6f}{row_logloss:>12.6f}")
    print()

    # ---- 3. by competition ----------------------------------------------------
    groups: dict[str, list[dict]] = defaultdict(list)
    for match in xg_matches:
        groups[match["competition"]].append(match)
    merged: dict[str, list[dict]] = {}
    other: list[dict] = []
    for name, matches in groups.items():
        if len(matches) < 3:
            other.extend(matches)
        else:
            merged[name] = matches
    if other:
        merged["其他"] = other

    print("===== 3. 按联赛分组（样本 < 3 归入 其他；差值 = w0.3 - w0）=====")
    print(f"{'联赛':<12}{'样本数':>8}{'Brier(w=0)':>14}{'Brier(w=0.3)':>15}{'差值':>12}")
    for name, matches in sorted(merged.items(), key=lambda item: (-len(item[1]), item[0])):
        b0 = sum(m["brier"][Decimal("0.0")] for m in matches) / len(matches)
        b3 = sum(m["brier"][EVAL_WEIGHT] for m in matches) / len(matches)
        print(f"{name:<12}{len(matches):>8}{b0:>14.6f}{b3:>15.6f}{b3 - b0:>+12.6f}")
    print()

    # ---- 4/5. worst and best individual matches ------------------------------
    def delta(match: dict) -> float:
        # Positive = blending made the Brier worse (xG hurt).
        return match["brier"][EVAL_WEIGHT] - match["brier"][Decimal("0.0")]

    print("===== 4. xG 帮倒忙最大的 5 场（ΔBrier = w0.3 - w0 最大）=====")
    print(f"{'match_id':<9}{'主队 vs 客队':<22}{'比分':<7}{'市场概率(主/平/客)':<24}{'模型概率(主/平/客)':<24}{'ΔBrier':>10}")
    for match in sorted(xg_matches, key=delta, reverse=True)[:5]:
        fixture = f'{match["home_team"]} vs {match["away_team"]}'
        print(
            f'{match["match_id"]:<9}{fixture:<22}{match["final_score"]:<7}'
            f'{fmt_probs(match["market"]):<24}{fmt_probs(match["model"]):<24}{delta(match):>+10.6f}'
        )
    print()

    print("===== 5. xG 帮助最大的 5 场（ΔBrier = w0.3 - w0 最小）=====")
    print(f"{'match_id':<9}{'主队 vs 客队':<22}{'比分':<7}{'市场概率(主/平/客)':<24}{'模型概率(主/平/客)':<24}{'ΔBrier':>10}")
    for match in sorted(xg_matches, key=delta)[:5]:
        fixture = f'{match["home_team"]} vs {match["away_team"]}'
        print(
            f'{match["match_id"]:<9}{fixture:<22}{match["final_score"]:<7}'
            f'{fmt_probs(match["market"]):<24}{fmt_probs(match["model"]):<24}{delta(match):>+10.6f}'
        )
    print()

    # ---- source split ---------------------------------------------------------
    print("===== 附：按 xG 来源拆分（Brier w=0 / w=0.3）=====")
    sources = Counter(match["source"] for match in xg_matches)
    for source, count in sorted(sources.items(), key=lambda item: (-item[1], item[0])):
        subset = [match for match in xg_matches if match["source"] == source]
        b0 = sum(m["brier"][Decimal("0.0")] for m in subset) / len(subset)
        b3 = sum(m["brier"][EVAL_WEIGHT] for m in subset) / len(subset)
        print(f"{source:<20}{count:>6} 场  Brier(w=0)={b0:.6f}  Brier(w=0.3)={b3:.6f}  Δ={b3 - b0:+.6f}")


if __name__ == "__main__":
    main()
