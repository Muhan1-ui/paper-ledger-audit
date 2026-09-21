# 中文仪表盘视觉重构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `subagent-driven-development` (recommended) or `executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将本地只读仪表盘重构为中文、专业且适合校招展示的后台界面，同时保持现有摘要接口和台账数据完全不变。

**Architecture:** 仅修改 `dashboard.html` 的 HTML、CSS 与浏览器端渲染函数。页面继续只请求 `/api/summary`，并将服务端返回的校准、策略、调度、证据与台账数据格式化为中文展示；Python 摘要层与 JSONL 台账均不改动。

**Tech Stack:** 静态 HTML、CSS、原生 JavaScript、Python `unittest`。

---

### Task 1: 锁定页面契约

**Files:**
- Modify: `work/tai_cai_tracker_tests/test_dashboard.py`

- [ ] **Step 1: 写出失败的页面文案与本地性测试**

在 `DashboardPageTests` 中验证页面保留 `/api/summary` 且不含外部 URL，并要求包含：`数据校准概览`、`策略表现对比`、`运行状态`、`证据状态`、`智能体流程`。同时要求页面不再将 `Operations` 或 `Agent workflow` 作为可见文案。

- [ ] **Step 2: 运行聚焦测试并确认失败**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_dashboard.DashboardPageTests
```

Expected: 因旧页面仍包含英文可见标题而失败。

### Task 2: 实现中文化展示与真实数据格式化

**Files:**
- Modify: `outputs/tai_cai_tracker/static/dashboard.html`

- [ ] **Step 1: 替换页面骨架和样式**

实现参考设计稿的三栏仪表盘：顶栏、侧栏导航、四个指标卡片、校准趋势与可靠性占位数据视图、策略与台账表格、右侧运行/证据/流程状态。所有静态可见文本使用简体中文，不添加远程字体、图片、脚本或接口。

- [ ] **Step 2: 格式化真实 API 字段**

为小数指标提供最多四位小数格式，为首选概率提供百分比格式，为调度年龄提供一位小数小时格式。把策略名称、记录类型和调度状态映射为中文展示标签，但保留原始 API 字段作为数据来源。

- [ ] **Step 3: 运行聚焦测试并确认通过**

Run:

```powershell
python -m unittest work.tai_cai_tracker_tests.test_dashboard.DashboardPageTests
```

Expected: `OK`。

### Task 3: 完整验证与只读检查

**Files:**
- Modify: `outputs/tai_cai_tracker/static/dashboard.html`
- Modify: `work/tai_cai_tracker_tests/test_dashboard.py`

- [ ] **Step 1: 运行完整测试套件**

Run:

```powershell
python -m unittest discover -s work/tai_cai_tracker_tests -t . -v
```

Expected: 所有测试通过。

- [ ] **Step 2: 运行只读报告验证账本**

Run:

```powershell
python -m outputs.tai_cai_tracker.cli report outputs/tai_cai_tracker/data/paper_ledger.jsonl --json
```

Expected: 成功输出校准报告，且不写入 JSONL。

- [ ] **Step 3: 检查页面网络边界与提交范围**

Run:

```powershell
rg -n "https?://" outputs/tai_cai_tracker/static/dashboard.html
git status --short
```

Expected: 前一命令无匹配；提交时只包含页面、测试和本计划，不包含用户的数据进度文件。
