# 跨电脑使用说明

## 迁移项目

将整个项目文件夹复制到另一台电脑。必须保留以下内容：

- `AGENTS.md`
- `START_NEW_CHAT.md`
- `PORTABLE_SETUP.md`
- `outputs`
- `work`
- `docs`

不要移动或单独改名台账文件：
`outputs/tai_cai_tracker/data/paper_ledger.jsonl`。它保存已冻结预测和已核验结算记录，必须保持相对路径不变。

项目中不包含账号凭据、浏览器配置、Cookie、私人 API 密钥或资金信息；不要把这些内容复制进项目文件夹。

## 接收电脑准备

1. 安装 Python 3.10 或更高版本：<https://www.python.org/downloads/windows/>。
2. 安装并登录该电脑自己的 Codex 账号。
3. 在 Codex 中打开复制后的项目根目录。
4. 新建任务，发送 `START_NEW_CHAT.md` 中的一句提示。

新账号不需要登录原账号。规则由项目根目录的 `AGENTS.md` 提供，历史状态由 JSONL 台账提供。

## 首次校验

在项目根目录运行：

```powershell
python -m outputs.tai_cai_tracker.cli report outputs/tai_cai_tracker/data/paper_ledger.jsonl
```

能输出台账报告即表示 Python 和历史数据已连接成功。若提示找不到 Python，先完成 Python 安装并重开终端或 Codex。

## 日常使用

每次都从这个项目目录创建或打开任务。需要跟踪赛事时，贴体彩官方链接；需要复盘时，直接发送“复盘昨天已完成比赛”。系统只做公开数据的纸面监测和模型复盘，不执行投注或资金操作。
