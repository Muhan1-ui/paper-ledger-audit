# TaiCaiTracker

面向中国体育彩票足球赛事的纸面数据审计与校准追踪系统。系统保留不可变的赛前记录，校验证据来源和时间，并在赛后复盘结果；不提供购买、资金或投注功能。

## 快速开始

```powershell
powershell -ExecutionPolicy Bypass -File work/start-dashboard.ps1
```

打开 `http://127.0.0.1:8765`。仪表盘仅在本机运行，且只读。

## 工程亮点

- Immutable JSONL ledger with linked prediction and settlement hashes.
- Evidence quality gates for official data, named odds references, xG context, and cutoff timing.
- Calibration reporting with Brier score, log loss, ECE, source cohorts, and sample gates.
- Agent workflow: collect evidence, validate constraints, freeze a paper record, settle only from official results.
- Standard-library localhost API with no external browser requests.

## 验证

```powershell
python -m unittest discover -s work/tai_cai_tracker_tests -p "test_*.py"
```

参见[架构说明](docs/architecture.md)和[项目规则](AGENTS.md)。
