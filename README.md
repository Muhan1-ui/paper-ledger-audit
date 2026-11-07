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

## MySQL 与 FastAPI 服务

原始 JSONL 台账仍是不可变审计事实源。MySQL 8 仅保存可重复同步的查询投影，供 FastAPI、仪表盘和 Agent 在线查询；同步不会覆盖或改写任何 JSONL 记录。

首次配置项目专用 MySQL 账号后，在项目根目录运行：

```powershell
python -m outputs.tai_cai_tracker.cli configure-database
python -m outputs.tai_cai_tracker.cli sync-service-projection outputs/tai_cai_tracker/data/paper_ledger.jsonl
python -m outputs.tai_cai_tracker.cli serve-api outputs/tai_cai_tracker/data/paper_ledger.jsonl
```

打开 `http://127.0.0.1:8000/` 查看仪表盘，或打开 `http://127.0.0.1:8000/docs` 查看和测试接口。当前提供：

- `GET /api/health`：服务与 MySQL 连通性检查。
- `GET /api/summary`：读取仪表盘所需的校准、证据、运行状态与最近台账活动。
- `GET /api/matches`：分页读取冻结赛事投影，返回总记录数与当前页。
- `GET /api/matches/{match_id}`：读取赛事的冻结预测、有效官方结算及已投影证据事件；已失效结算不会作为事实返回。
- `GET /api/reviews`：读取全局只读复盘摘要与校准样本状态。
- `GET /api/matches/{match_id}/review`：读取单场的已验证事实、冻结模型输出、官方结算和数据缺口。

数据库密码保存在项目根目录、已被 Git 忽略的 `.env` 文件中。生产环境可通过同名环境变量覆盖本地配置。

需要使用 Docker 部署应用服务时，参见[部署说明](docs/deployment.md)。Docker 不会启动或替换现有 MySQL，只会连接并同步查询投影。

服务启动后可运行以下命令做只读验收；Docker 使用 `8002` 等非默认端口时传入对应端口：

```powershell
powershell -ExecutionPolicy Bypass -File work/verify-service.ps1
powershell -ExecutionPolicy Bypass -File work/verify-service.ps1 -Port 8002
```

用于简历撰写、面试演示和技术问答的材料见[简历与面试材料](docs/resume-interview.md)。

## 验证

```powershell
python -m unittest discover -s work/tai_cai_tracker_tests -p "test_*.py"
```

参见[架构说明](docs/architecture.md)和[项目规则](AGENTS.md)。
