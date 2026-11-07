# 部署说明

## 设计边界

本项目的 JSONL 台账是不可变审计事实源，Docker 容器只以只读方式挂载该文件。MySQL 仅保存可重复生成的查询投影，容器每次启动都会先执行同步，再启动 FastAPI 服务。

Compose 文件不会创建新的 MySQL 容器，不会覆盖本机已有数据库、账号或密码。它适合 Windows 本机演示和将应用部署到服务器；你目前的本机 Python 启动方式仍然可用。

## 准备配置

1. 安装 Docker Desktop，并确认 Docker Compose 可用。
2. 复制 `.env.example` 为项目根目录的 `.env`。若你已经有 `.env`，保留现有 MySQL 和 DeepSeek 配置，只补充 `MYSQL_HOST_DOCKER`。
3. Windows Docker Desktop 默认使用 `host.docker.internal` 访问宿主机 MySQL，示例配置已预设该值。
4. Linux 服务器默认通过 Compose 的 `host-gateway` 映射访问宿主机 MySQL；若 MySQL 位于独立服务器，请将 `MYSQL_HOST_DOCKER` 设置为该服务器地址，并为应用服务器配置最小化访问权限。

不要提交 `.env`，不要把 API Key、MySQL 密码或台账副本写入镜像。

## 启动

在项目根目录运行：

```powershell
docker compose config --quiet
docker compose up --build -d
docker compose ps
```

浏览器打开 `http://127.0.0.1:8000/` 查看仪表盘，`http://127.0.0.1:8000/docs` 查看接口文档。

如果 `8000` 已被本机服务占用，可临时指定其他宿主机端口：

```powershell
$env:TAICAI_PORT = "8002"
docker compose up --build -d
```

此时访问 `http://127.0.0.1:8002/`。

首次启动日志应先出现投影同步统计，再出现 Uvicorn 服务启动信息。查看日志：

```powershell
docker compose logs -f taicai-service
```

停止容器但保留 MySQL 与台账：

```powershell
docker compose down
```

## 本机直接启动

不使用 Docker 时，继续使用现有环境即可：

```powershell
python -m outputs.tai_cai_tracker.cli sync-service-projection outputs/tai_cai_tracker/data/paper_ledger.jsonl
python -m outputs.tai_cai_tracker.cli serve-api outputs/tai_cai_tracker/data/paper_ledger.jsonl
```

## 上线检查

- JSONL 文件以只读方式挂载，且未被容器改写。
- `/api/health` 返回 MySQL 连通状态。
- `/api/reviews` 返回 `read_only` 模式。
- 将 `8000` 端口仅暴露给受控反向代理或内网，不直接公开数据库端口。
