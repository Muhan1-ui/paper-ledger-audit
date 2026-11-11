#!/usr/bin/env python
"""Idempotent settlement-time evaluation report for the paper ledger.

Read-only with respect to the ledger.  Writes only:
  work/eval-reports/history.jsonl   one line per calendar date
  work/eval-reports/latest.md       the latest human-readable report

Data loading, fixture association, invalidation filtering, de-vigging and
Decimal normalisation are reused from work/eval_market_source.py and
work/eval_xg_blend.py rather than reimplemented.

Two evaluation universes are kept strictly apart:

  prematch  only probabilities available before kickoff -- the only universe
            that may be used to judge model quality
  leak      anything blended with post-match FotMob single-match xG -- must
            never be read as a model-evaluation result
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

LEDGER_PATH = ROOT / "outputs" / "tai_cai_tracker" / "data" / "paper_ledger.jsonl"
BACKFILL_PATH = ROOT / "outputs" / "tai_cai_tracker" / "data" / "fotmob_xg_backfill.jsonl"
REPORT_DIR = ROOT / "work" / "eval-reports"
HISTORY_PATH = REPORT_DIR / "history.jsonl"
LATEST_PATH = REPORT_DIR / "latest.md"

LABELS = ("home_win", "draw", "away_win")
WEIGHT_PREMATCH_XG = Decimal("0.3")
LEAK_WEIGHTS = [Decimal("0.1"), Decimal("0.2"), Decimal("0.3"),
                Decimal("0.4"), Decimal("0.5")]
Z_ALPHA = 1.959963984540054   # two-sided 5%
Z_POWER = 0.8416212335729143  # 80% power
TARGET_DELTA = 0.01

SCHEME_NAMES = {
    "china": "体彩固定奖金（去水）",
    "europe": "欧洲赔率（去水，赛前最新）",
    "xg_pre": "台账 xg_inputs 赛前融合 w=0.3",
}


def _load_module(name: str, filename: str):
    path = ROOT / "work" / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


MARKET = _load_module("eval_market_source_shared", "eval_market_source.py")
XGBLEND = _load_module("eval_xg_blend_shared", "eval_xg_blend.py")

from outputs.tai_cai_tracker.model import (  # noqa: E402
    blend_probabilities,
    poisson_score_matrix,
)


# ---------------------------------------------------------------------------
# metric primitives
# ---------------------------------------------------------------------------
def mean(values: list[float]) -> float:
    return sum(values) / len(values)


def sample_sd(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    centre = mean(values)
    return math.sqrt(sum((value - centre) ** 2 for value in values) / (n - 1))


def ece(pairs: list[tuple[dict[str, Decimal], str]], bins: int = 10) -> float:
    """Multiclass ECE over flattened (probability, hit) pairs.

    Each sample contributes its three label probabilities; each pair lands in
    one equal-width bin [0, 0.1) ... [0.9, 1.0].  Bins are weighted by pair
    count, so the result is a single number for the 3-class distribution.
    """
    if not pairs:
        return float("nan")
    totals = [0.0] * bins
    hits = [0.0] * bins
    counts = [0] * bins
    for probabilities, outcome in pairs:
        for label in LABELS:
            probability = min(1.0, max(0.0, float(probabilities[label])))
            index = min(int(probability * bins), bins - 1)
            totals[index] += probability
            hits[index] += 1.0 if label == outcome else 0.0
            counts[index] += 1
    pair_count = sum(counts)
    error = 0.0
    for index in range(bins):
        if not counts[index]:
            continue
        average_probability = totals[index] / counts[index]
        hit_rate = hits[index] / counts[index]
        error += counts[index] / pair_count * abs(average_probability - hit_rate)
    return error


def summarize(pairs: list[tuple[dict[str, Decimal], str]]) -> dict:
    n = len(pairs)
    if not n:
        return {"n": 0, "brier": None, "logloss": None, "ece": None,
                "brier_sd": None, "brier_se": None}
    briers = [MARKET.brier(probabilities, outcome) for probabilities, outcome in pairs]
    loglosses = [MARKET.log_loss(probabilities, outcome) for probabilities, outcome in pairs]
    sd = sample_sd(briers)
    return {
        "n": n,
        "brier": mean(briers),
        "logloss": mean(loglosses),
        "ece": ece(pairs),
        "brier_sd": sd,
        "brier_se": sd / math.sqrt(n) if n else None,
    }


def public(metrics: dict) -> dict:
    return {key: metrics[key] for key in
            ("n", "brier", "logloss", "ece", "brier_sd", "brier_se")}


def fmt(value, digits: int = 6) -> str:
    return "N/A" if value is None else f"{value:.{digits}f}"


def md_table(headers: list[str], rows: list[list[str]],
             aligns: list[str] | None = None) -> list[str]:
    aligns = aligns or ["l"] + ["r"] * (len(headers) - 1)
    lines = ["| " + " | ".join(headers) + " |"]
    lines.append("| " + " | ".join("---:" if a == "r" else "---" for a in aligns) + " |")
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return lines


# ---------------------------------------------------------------------------
# ledger association
# ---------------------------------------------------------------------------
def valid_settlement_for(prediction: dict,
                         settlements_by_prediction: dict[str, list[dict]],
                         invalidated: set[str]) -> tuple[dict | None, str | None, str, bool]:
    """Last non-invalidated settlement that yields an outcome."""
    settlements = settlements_by_prediction.get(prediction.get("content_hash"), [])
    for settlement in reversed(settlements):
        if settlement.get("content_hash") in invalidated:
            continue
        outcome, score_text, used_final_score = MARKET.settlement_outcome(settlement)
        if outcome is not None:
            return settlement, outcome, score_text, used_final_score
    return None, None, "", False


def european_for(prediction: dict,
                 captures_by_match: dict[tuple, list[dict]]) -> tuple[dict | None, str]:
    kickoff = MARKET.parse_time(prediction.get("kickoff_at"))
    captured = MARKET.parse_time(prediction.get("captured_at"))
    if kickoff is None or captured is None:
        return None, "unattributed"
    entries = captures_by_match.get((prediction.get("match_id"), kickoff), [])
    usable = [entry for entry in entries if entry["captured"] <= captured]
    if not usable:
        return None, "unattributed"
    capture = usable[-1]["capture"]
    return (
        MARKET.odds_to_probabilities(capture.get("odds")),
        capture.get("bookmaker") or "unattributed",
    )


def backfill_for(prediction: dict, backfill_by_key: dict[tuple, list[dict]]) -> dict | None:
    """Same fixture matcher as work/eval_xg_blend.py (kickoff proximity guard)."""
    context = prediction.get("match_context") or {}
    candidates = backfill_by_key.get(
        (prediction.get("match_id"), context.get("home_team")), []
    )
    if not candidates:
        return None
    if len(candidates) == 1:
        only = candidates[0]
        prediction_kickoff = XGBLEND.kickoff(prediction.get("kickoff_at"))
        backfill_kickoff = XGBLEND.kickoff(only.get("kickoff_at"))
        if prediction_kickoff and backfill_kickoff:
            if abs((prediction_kickoff - backfill_kickoff).total_seconds()) > 36 * 3600:
                return None
        return only
    prediction_kickoff = XGBLEND.kickoff(prediction.get("kickoff_at"))
    ranked = sorted(
        candidates,
        key=lambda row: abs(
            (prediction_kickoff - XGBLEND.kickoff(row.get("kickoff_at"))).total_seconds()
        )
        if prediction_kickoff and XGBLEND.kickoff(row.get("kickoff_at"))
        else float("inf"),
    )
    return ranked[0]


def xg_probabilities(home_xg, away_xg, market: dict, weight: Decimal | None) -> dict:
    model = poisson_score_matrix(
        Decimal(str(home_xg)), Decimal(str(away_xg))
    )["one_x_two"]
    model = XGBLEND._normalised_dict(model)
    if weight is None:
        return model
    return blend_probabilities(market, model, weight)


# ---------------------------------------------------------------------------
# report persistence
# ---------------------------------------------------------------------------
def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent,
        prefix=f".{path.name}.", suffix=".tmp", delete=False,
    )
    try:
        with handle:
            handle.write(text)
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def update_history(entry: dict) -> tuple[int, int]:
    """Replace the line for entry['date']; return (previous_lines, new_lines)."""
    kept: list[str] = []
    previous = 0
    if HISTORY_PATH.exists():
        for line in HISTORY_PATH.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            previous += 1
            try:
                existing = json.loads(line)
            except json.JSONDecodeError:
                kept.append(line)  # never destroy a line we cannot parse
                continue
            if existing.get("date") == entry["date"]:
                continue
            kept.append(line)
    kept.append(json.dumps(entry, ensure_ascii=False, sort_keys=True))
    atomic_write(HISTORY_PATH, "\n".join(kept) + "\n")
    return previous, len(kept)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> None:
    records = XGBLEND.load_jsonl(LEDGER_PATH)
    backfill = XGBLEND.load_jsonl(BACKFILL_PATH)

    invalidated = {
        record["settlement_hash"]
        for record in records
        if record.get("record_type") == "settlement_invalidation"
        and record.get("settlement_hash")
    }

    settlements_by_prediction: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        if record.get("record_type") == "settlement" and record.get("prediction_hash"):
            settlements_by_prediction[record["prediction_hash"]].append(record)

    # primary freezes, deduplicated by the actual fixture.  match_id repeats
    # across weeks; kickoff_at separates rounds; captured_at keeps the later
    # record when a freeze was repeated byte-for-byte.
    raw_primaries = [
        record for record in records
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

    # European captures, keyed by (match_id, kickoff instant), sorted by time.
    captures_by_match: dict[tuple, list[dict]] = defaultdict(list)
    for capture in records:
        if capture.get("record_type") != "odds_capture":
            continue
        kickoff = MARKET.parse_time(capture.get("kickoff_at"))
        captured = MARKET.parse_time(capture.get("captured_at"))
        if kickoff is None or captured is None or not capture.get("match_id"):
            continue
        captures_by_match[(capture["match_id"], kickoff)].append(
            {"capture": capture, "captured": captured}
        )
    for entries in captures_by_match.values():
        entries.sort(key=lambda entry: entry["captured"])

    backfill_by_key: dict[tuple, list[dict]] = defaultdict(list)
    for row in backfill:
        backfill_by_key[(row.get("ledger_match_id"), row.get("home_team"))].append(row)

    samples: list[dict] = []
    stats = Counter()
    for prediction in primaries:
        settlement, outcome, score_text, used_final_score = valid_settlement_for(
            prediction, settlements_by_prediction, invalidated
        )
        if settlement is None:
            stats["unsettled"] += 1
            continue
        if not used_final_score:
            stats["actual_outcome_only"] += 1

        context = prediction.get("match_context") or {}
        china = MARKET.prizes_to_probabilities(prediction.get("fixed_prizes"))
        europe, bookmaker = european_for(prediction, captures_by_match)

        xg_pre = None
        own_xg = prediction.get("xg_inputs") or {}
        has_own_xg = (
            own_xg.get("home_xg") is not None and own_xg.get("away_xg") is not None
        )
        if has_own_xg and china is not None:
            xg_pre = xg_probabilities(
                own_xg["home_xg"], own_xg["away_xg"], china, WEIGHT_PREMATCH_XG
            )
            stats["xg_pre_used"] += 1
        elif has_own_xg:
            stats["xg_pre_no_china_market"] += 1

        leak_xg = None
        if not has_own_xg:
            row = backfill_for(prediction, backfill_by_key)
            xg = (row or {}).get("xg") or {}
            if xg.get("home_xg") is not None and xg.get("away_xg") is not None:
                leak_xg = (xg["home_xg"], xg["away_xg"])
                stats["leak_xg_used"] += 1

        samples.append({
            "match_id": prediction.get("match_id"),
            "home_team": context.get("home_team") or "?",
            "away_team": context.get("away_team") or "?",
            "competition": context.get("competition") or "其他",
            "outcome": outcome,
            "score": score_text,
            "china": china,
            "europe": europe,
            "bookmaker": bookmaker,
            "xg_pre": xg_pre,
            "leak_xg": leak_xg,
        })

    prematch_samples = [sample for sample in samples
                        if sample["china"] or sample["europe"] or sample["xg_pre"]]
    common_all = [
        sample for sample in samples
        if sample["china"] and sample["europe"] and sample["xg_pre"]
    ]

    full_pairs = {
        "china": [(s["china"], s["outcome"]) for s in samples if s["china"]],
        "europe": [(s["europe"], s["outcome"]) for s in samples if s["europe"]],
        "xg_pre": [(s["xg_pre"], s["outcome"]) for s in samples if s["xg_pre"]],
    }
    full_metrics = {key: summarize(pairs) for key, pairs in full_pairs.items()}
    common_metrics = {
        key: summarize([(s[key], s["outcome"]) for s in common_all])
        for key in ("china", "europe", "xg_pre")
    }

    # Best prematch scheme is chosen on the common sample so the comparison is
    # not confounded by xg_inputs coverage.  Full-sample metrics are still shown.
    ranking = [key for key in ("china", "europe", "xg_pre")
               if common_metrics[key]["n"]]
    best_key = min(ranking, key=lambda key: common_metrics[key]["brier"]) if ranking else "china"

    now = datetime.now().astimezone()
    date_text = now.date().isoformat()
    generated_at = now.isoformat(timespec="seconds")
    lines: list[str] = []
    lines.append("# 台彩 Paper 模型评测报表")
    lines.append("")
    lines.append(f"生成时间：{generated_at}")
    lines.append(f"台账：`{LEDGER_PATH.relative_to(ROOT)}`（只读，未改动）")
    lines.append("")

    # ---- 0. samples -------------------------------------------------------
    lines.append("## 0. 样本")
    lines.append("")
    lines.append(
        f"primary 预测 {len(raw_primaries)} 条，同场次重复冻结去重 {duplicate_groups} 条，"
        f"去重后 {len(primaries)} 场；其中有效结算 {len(samples)} 场。"
    )
    lines.append("")
    lines.append(f"被 settlement_invalidation 作废的结算已排除；仅凭 actual_outcome "
                 f"而无 final_score 的样本 {stats['actual_outcome_only']} 场。")
    lines.append("")
    lines.extend(md_table(
        ["项目", "样本数"],
        [
            ["prematch：体彩固定奖金可用", len(full_pairs["china"])],
            ["prematch：欧洲赔率可用（赛前配对，captured_at ≤ 冻结时间）", len(full_pairs["europe"])],
            ["prematch：台账 xg_inputs 融合可用", len(full_pairs["xg_pre"])],
            ["prematch：三组共同样本", len(common_all)],
            ["leak：FotMob 赛后 xG 回填可用", sum(1 for s in samples if s["leak_xg"])],
        ],
        ["l", "r"],
    ))
    lines.append("")
    lines.append("口径：**prematch** = 仅赛前可得的概率，是唯一可用于判断模型好坏的口径；"
                 "**leak** = 含赛后信息，只能单列诊断。")
    lines.append("")

    # ---- A. prematch baselines --------------------------------------------
    lines.append("## A. prematch 三组 baseline")
    lines.append("")
    lines.append("### A1. 各组可用样本")
    lines.append("")
    rows = []
    for key in ("china", "europe", "xg_pre"):
        metrics = full_metrics[key]
        rows.append([
            SCHEME_NAMES[key], metrics["n"], fmt(metrics["brier"]),
            fmt(metrics["logloss"]), fmt(metrics["ece"]),
        ])
    lines.extend(md_table(["方案", "样本数", "Brier", "LogLoss", "ECE"], rows))
    lines.append("")
    lines.append("### A2. 三组共同样本（同场同结果，可直接比较）")
    lines.append("")
    rows = []
    for key in ("china", "europe", "xg_pre"):
        metrics = common_metrics[key]
        rows.append([
            SCHEME_NAMES[key], metrics["n"], fmt(metrics["brier"]),
            fmt(metrics["logloss"]), fmt(metrics["ece"]),
        ])
    lines.extend(md_table(["方案", "样本数", "Brier", "LogLoss", "ECE"], rows))
    lines.append("")
    lines.append(f"共同样本 Brier 最优方案：**{SCHEME_NAMES[best_key]}**"
                 f"（Brier {fmt(common_metrics[best_key]['brier'])}）。")
    lines.append("")
    lines.append("ECE 口径：10 个等宽分箱 [0,0.1)…[0.9,1.0]，每个样本展开为 3 个 "
                 "(p, hit) 对，按箱内样本数加权求 |平均 p − 实际命中率| 之和。")
    lines.append("")

    # ---- B. by competition ------------------------------------------------
    lines.append("## B. 按联赛拆分（样本 < 3 归入“其他”）")
    lines.append("")
    lines.append(f"基线 = 体彩固定奖金去水；最优方案 = {SCHEME_NAMES[best_key]}。"
                 "每行只使用两者都可用且已结算的样本。")
    lines.append("")
    comparison_samples = [
        sample for sample in samples
        if sample["china"] and sample[best_key]
    ]
    competition_groups: dict[str, list[dict]] = defaultdict(list)
    for sample in comparison_samples:
        competition_groups[sample["competition"]].append(sample)
    merged: dict[str, list[dict]] = {}
    other: list[dict] = []
    for name, members in competition_groups.items():
        if len(members) < 3:
            other.extend(members)
        else:
            merged[name] = members
    if other:
        merged["其他"] = other

    rows = []
    for name, members in sorted(merged.items(), key=lambda item: (-len(item[1]), item[0])):
        baseline_brier = mean([MARKET.brier(m["china"], m["outcome"]) for m in members])
        best_brier = mean([MARKET.brier(m[best_key], m["outcome"]) for m in members])
        rows.append([name, len(members), fmt(baseline_brier), fmt(best_brier),
                     f"{best_brier - baseline_brier:+.6f}"])
    lines.extend(md_table(
        ["联赛", "样本数", "Brier（基线）", f"Brier（{SCHEME_NAMES[best_key]}）", "差值（最优−基线）"],
        rows,
    ))
    lines.append("")

    # ---- C. by bookmaker --------------------------------------------------
    lines.append("## C. 欧洲赔率组按 bookmaker 拆分（样本 < 3 归入“其他”）")
    lines.append("")
    bookmaker_groups: dict[str, list[dict]] = defaultdict(list)
    for sample in samples:
        if sample["europe"]:
            bookmaker_groups[sample["bookmaker"]].append(sample)
    bookmaker_rows: list[list] = []
    other_bookmakers: list[dict] = []
    for name, members in bookmaker_groups.items():
        if len(members) < 3:
            other_bookmakers.extend(members)
        else:
            bookmaker_rows.append([
                name, len(members),
                fmt(mean([MARKET.brier(m["europe"], m["outcome"]) for m in members])),
            ])
    bookmaker_rows.sort(key=lambda row: (-row[1], row[0]))
    if other_bookmakers:
        bookmaker_rows.append([
            "其他", len(other_bookmakers),
            fmt(mean([MARKET.brier(m["europe"], m["outcome"]) for m in other_bookmakers])),
        ])
    lines.extend(md_table(["bookmaker", "样本数", "Brier"], bookmaker_rows))
    lines.append("")

    # ---- D. leak ----------------------------------------------------------
    leak_samples = [sample for sample in samples
                    if sample["leak_xg"] and sample["china"]]
    leak_weight_rows: list[dict] = []
    lines.append("## D. leak 组：FotMob 赛后单场 xG 融合（单独成表）")
    lines.append("")
    lines.append("> **含赛后信息，不可用于模型评估。** 本节 xG 取自 FotMob 赛后比赛数据，"
                 "结果已知，只用于诊断回填数据与模型的机械表现，禁止与 A/B/C 节混读。")
    lines.append("")
    lines.append(
        f"市场基准为体彩固定奖金去水，模型为赛后 xG 的 Poisson 1X2，按 w 融合；"
        f"样本 {len(leak_samples)} 场。"
    )
    lines.append("")
    rows = []
    base_pairs = [(s["china"], s["outcome"]) for s in leak_samples]
    base_metrics = summarize(base_pairs)
    rows.append(["w=0.0（无 xG，参考）", base_metrics["n"],
                 fmt(base_metrics["brier"]), fmt(base_metrics["logloss"])])
    for weight in LEAK_WEIGHTS:
        pairs = []
        for sample in leak_samples:
            probabilities = xg_probabilities(
                sample["leak_xg"][0], sample["leak_xg"][1], sample["china"], weight
            )
            pairs.append((probabilities, sample["outcome"]))
        metrics = summarize(pairs)
        leak_weight_rows.append({
            "weight": f"{float(weight):.1f}",
            "n": metrics["n"],
            "brier": metrics["brier"],
            "logloss": metrics["logloss"],
        })
        rows.append([f"w={float(weight):.1f}", metrics["n"],
                     fmt(metrics["brier"]), fmt(metrics["logloss"])])
    lines.extend(md_table(["方案（leak）", "样本数", "Brier", "LogLoss"], rows))
    lines.append("")

    # ---- E. sample size ---------------------------------------------------
    lines.append("## E. 样本量与可检测差异")
    lines.append("")
    lines.append("Brier 标准误 = 样本 Brier 标准差 / √n。")
    lines.append("")
    rows = []
    for key in ("china", "europe", "xg_pre"):
        metrics = full_metrics[key]
        rows.append([
            SCHEME_NAMES[key], metrics["n"], fmt(metrics["brier"]),
            fmt(metrics["brier_sd"]), fmt(metrics["brier_se"]),
        ])
    lines.extend(md_table(
        ["方案", "样本数", "平均 Brier", "样本 Brier 标准差", "标准误"], rows
    ))
    lines.append("")

    s_china = full_metrics["china"]["brier_sd"] or 0.0
    s_europe = full_metrics["europe"]["brier_sd"] or 0.0
    pooled_sd = math.sqrt((s_china ** 2 + s_europe ** 2) / 2)
    n_unpaired = math.ceil(
        2 * (Z_ALPHA + Z_POWER) ** 2 * pooled_sd ** 2 / TARGET_DELTA ** 2
    ) if pooled_sd else None

    paired_china_europe = [
        MARKET.brier(s["china"], s["outcome"]) - MARKET.brier(s["europe"], s["outcome"])
        for s in samples if s["china"] and s["europe"]
    ]
    sd_diff_ce = sample_sd(paired_china_europe)
    n_paired_ce = math.ceil(
        ((Z_ALPHA + Z_POWER) * sd_diff_ce / TARGET_DELTA) ** 2
    ) if sd_diff_ce else None

    paired_china_xg = [
        MARKET.brier(s["china"], s["outcome"]) - MARKET.brier(s["xg_pre"], s["outcome"])
        for s in samples if s["china"] and s["xg_pre"]
    ]
    sd_diff_cx = sample_sd(paired_china_xg)
    n_paired_cx = math.ceil(
        ((Z_ALPHA + Z_POWER) * sd_diff_cx / TARGET_DELTA) ** 2
    ) if sd_diff_cx else None

    lines.append("能检测到 **0.01 Brier 差异** 所需样本量（双侧 α=0.05，功效 80%）：")
    lines.append("")
    lines.append(f"- 非配对两组比较（每组样本 Brier 标准差约 {fmt(pooled_sd, 4)}）："
                 f"每组约 **{n_unpaired:,}** 场。" if n_unpaired
                 else "- 非配对两组比较：当前标准差不可用，无法估计。")
    lines.append(f"- 配对比较 体彩 vs 欧洲（共同 {len(paired_china_europe)} 场，"
                 f"差值标准差 {fmt(sd_diff_ce, 4)}）：约 **{n_paired_ce:,}** 对。" if n_paired_ce
                 else "- 配对比较 体彩 vs 欧洲：差值标准差不可用，无法估计。")
    lines.append(f"- 配对比较 体彩 vs 台账 xG（共同 {len(paired_china_xg)} 场，"
                 f"差值标准差 {fmt(sd_diff_cx, 4)}）：约 **{n_paired_cx:,}** 对。" if n_paired_cx
                 else "- 配对比较 体彩 vs 台账 xG：差值标准差不可用，无法估计。")
    lines.append("")
    if len(paired_china_europe) > 1:
        paired_se = sd_diff_ce / math.sqrt(len(paired_china_europe))
        detectable = (Z_ALPHA + Z_POWER) * paired_se
        lines.append(f"当前 {len(paired_china_europe)} 场配对样本下，体彩 vs 欧洲的"
                     f"可检测最小差异约 **{fmt(detectable, 6)}**（80% 功效）。"
                     "小于该值的差异不应解读为方案优劣。")
        lines.append("")
    lines.append(f"当前有效结算样本 {len(samples)} 场；"
                 "样本量门槛（< 50）未到时，所有差异只能视为描述性。")
    lines.append("")

    report_text = "\n".join(lines) + "\n"
    print(report_text, end="")
    atomic_write(LATEST_PATH, report_text)

    entry = {
        "date": date_text,
        "generated_at": generated_at,
        "ledger": str(LEDGER_PATH.relative_to(ROOT)),
        "samples": {
            "primary_records": len(raw_primaries),
            "duplicate_groups": duplicate_groups,
            "deduped_primary": len(primaries),
            "settled": len(samples),
            "prematch_samples": len(prematch_samples),
            "common_all_prematch": len(common_all),
            "leak_samples": len(leak_samples),
        },
        "prematch": {
            "schemes": {key: public(full_metrics[key])
                        for key in ("china", "europe", "xg_pre")},
            "common_samples": {key: public(common_metrics[key])
                               for key in ("china", "europe", "xg_pre")},
            "best_scheme": best_key,
            "by_competition": merged and [
                {
                    "competition": name,
                    "n": len(members),
                    "brier_baseline": mean(
                        [MARKET.brier(m["china"], m["outcome"]) for m in members]
                    ),
                    "brier_best": mean(
                        [MARKET.brier(m[best_key], m["outcome"]) for m in members]
                    ),
                }
                for name, members in sorted(
                    merged.items(), key=lambda item: (-len(item[1]), item[0])
                )
            ],
        },
        "by_bookmaker": [
            {
                "bookmaker": row[0],
                "n": row[1],
                "brier": float(row[2]),
            }
            for row in bookmaker_rows
        ],
        "leak": {
            "warning": "含赛后信息，不可用于模型评估",
            "n": len(leak_samples),
            "weights": leak_weight_rows,
        },
        "sample_size": {
            "by_scheme": {key: {
                "n": full_metrics[key]["n"],
                "brier_sd": full_metrics[key]["brier_sd"],
                "brier_se": full_metrics[key]["brier_se"],
            } for key in ("china", "europe", "xg_pre")},
            "detect_0.01": {
                "unpaired_per_group": n_unpaired,
                "paired_china_vs_europe": n_paired_ce,
                "paired_china_vs_xg": n_paired_cx,
                "paired_diff_sd_china_vs_europe": sd_diff_ce,
                "paired_diff_sd_china_vs_xg": sd_diff_cx,
            },
        },
    }
    previous, new_count = update_history(entry)
    print(f"history: {HISTORY_PATH.relative_to(ROOT)} 原有 {previous} 行，"
          f"按 date={date_text} 去重后 {new_count} 行。")


if __name__ == "__main__":
    main()
