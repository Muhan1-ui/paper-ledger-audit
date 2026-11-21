"""Cross-check daily_slip's SPF probabilities against frozen blended_outcomes.

Read-only. Uses the real paper ledger for the 2026-11-15 replay only.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from outputs.tai_cai_tracker.daily_slip import (  # noqa: E402
    hhad_probabilities,
    normalize_scorelines,
    spf_probabilities,
    ttg_probabilities,
)

LEDGER = Path(__file__).resolve().parents[1] / "outputs/tai_cai_tracker/data/paper_ledger.jsonl"
DAY = "2026-11-15"

records = [json.loads(line) for line in LEDGER.read_text(encoding="utf-8").splitlines() if line.strip()]
predictions = [
    record
    for record in records
    if record.get("record_type") == "prediction"
    and record.get("kickoff_at", "")[:10] == DAY
    and isinstance(record.get("model"), dict)
]

print(f"{DAY} 有模型预测 {len(predictions)} 场")
print(f"{'场次':<8} {'SPF模块':<24} {'blended原值':<24} {'最大误差':>8}")
worst = 0.0
for prediction in predictions:
    model = prediction["model"]
    module_spf = spf_probabilities(model)
    raw_spf = {key: float(value) for key, value in model["blended_outcomes"].items()}
    error = max(abs(module_spf[key] - raw_spf[key]) for key in raw_spf)
    worst = max(worst, error)
    print(
        f"{prediction['match_id']:<8} "
        f"{'/'.join(f'{module_spf[k]:.6f}' for k in ('home_win','draw','away_win')):<24} "
        f"{'/'.join(f'{raw_spf[k]:.6f}' for k in ('home_win','draw','away_win')):<24} "
        f"{error:>8.2e}"
    )

print(f"\n最大绝对误差 {worst:.3e}  -> {'PASS' if worst < 0.001 else 'FAIL'} (阈值 0.001)")

# Why the module does not derive SPF from scorelines: show that the scoreline
# aggregate equals model_outcomes, not the frozen blended SPF.
print("\n说明：若用 scorelines 聚合，得到的是 model_outcomes，不是 blended_outcomes。")
first = predictions[0]
scores = normalize_scorelines(first["model"])
spf_from_scores = {key: 0.0 for key in ("home_win", "draw", "away_win")}
for key, probability in scores.items():
    home, away = (int(part) for part in key.split(":"))
    spf_from_scores["home_win" if home > away else "draw" if home == away else "away_win"] += probability
print(f"{first['match_id']} scorelines 聚合: " + "/".join(f"{v:.6f}" for v in spf_from_scores.values()))
ordered = ("home_win", "draw", "away_win")
print(
    f"{first['match_id']} model_outcomes : "
    + "/".join(f"{float(first['model']['model_outcomes'][k]):.6f}" for k in ordered)
)
print(
    f"{first['match_id']} blended_outcomes: "
    + "/".join(f"{float(first['model']['blended_outcomes'][k]):.6f}" for k in ordered)
)

# Sanity: the other markets sum to one and the CRS residual is explicit.
hh = hhad_probabilities(scores, -1)
tt = ttg_probabilities(scores)
print(f"HHAD(-1) 和 {sum(hh.values()):.6f}   TTG 和 {sum(tt.values()):.6f}")
