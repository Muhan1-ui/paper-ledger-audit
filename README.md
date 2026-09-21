# TaiCaiTracker

面向中国体育彩票足球赛事的纸面数据审计与校准追踪系统。系统保留不可变的赛前记录，校验证据来源和时间，并在赛后复盘结果；不提供购买、资金或投注功能。

## 快速开始

```powershell
powershell -ExecutionPolicy Bypass -File work/start-dashboard.ps1
```

打开 `http://127.0.0.1:8765`。仪表盘仅在本机运行，且只读。

## 工程亮点

- 基于 JSONL 的不可变台账，预测与结算记录通过内容哈希关联。
- 对官方数据、具名赔率参考、xG 上下文和截止时间设置证据质量门禁。
- 输出 Brier Score、Log Loss、ECE、来源分组和样本门槛等校准报告。
- Agent 工作流：采集证据、校验约束、冻结纸面记录、仅以官方赛果结算。
- 基于 Python 标准库的 localhost API，浏览器不请求外部资源。

## 验证

```powershell
python -m unittest discover -s work/tai_cai_tracker_tests -p "test_*.py"
```

参见[架构说明](docs/architecture.md)和[项目规则](AGENTS.md)。
