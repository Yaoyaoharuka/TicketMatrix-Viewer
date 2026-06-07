# TicketMatrix Viewer

NOL 进度查询 —— 移动端友好的飞书表格进度查看应用。从 `TicketMatrix` 抢票项目中拆分而来，只负责展示，不参与抢票逻辑。

## 功能

- 选择场次 + 账号密码登录，查看实时进度
- 账号状态关键词分类（排队 / 扫描 / 完成 / 异常 / 休息 / 等待中）
- 登录后顶部下拉切换场次
- 管理员总览：各服务器账号数 / 异常数统计
- 数据每 3 分钟自动刷新，右下角手动刷新按钮

## 架构

- **后端** `mobile_app.py`：基于 Python 标准库 `http.server`，提供 `/api/login`、`/api/data`、`/api/sheets`、`/api/ip-location` 接口，并托管前端静态文件。
- **前端** `web/index.html`：单文件 HTML/CSS/JS，无框架、无构建。
- **数据源**：飞书表格，经 `tools/feishu_sheet.py` 读取（只读子集）。凭证和表格 token 统一配置在 `.env`。
- 与抢票核心**零代码耦合**，仅以飞书表格作为数据中转。

## 依赖

```bash
pip install -r requirements.txt
```

## 配置

仓库不含任何真实凭证或 token，首次使用需准备本地 `.env` 文件（已被 `.gitignore` 忽略）：

**`.env`**（从 `.env.example` 复制）—— 飞书凭证、表格 token 与管理员密码：

```bash
cp .env.example .env
# 然后填入 FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_SHEET_* / VIEWER_ADMIN_PASSWORD
```

| 变量 | 必填 | 说明 |
|---|---|---|
| `FEISHU_APP_ID` | 是 | 飞书应用 ID（缺失则无法读表） |
| `FEISHU_APP_SECRET` | 是 | 飞书应用 Secret |
| `FEISHU_POLL_INTERVAL` | 否 | 飞书表格轮询间隔，默认 `60` 秒 |
| `FEISHU_SERVERS_TOKEN` | 否 | 服务器表 spreadsheet/wiki token，用于管理员服务器汇总 |
| `FEISHU_SHEET_<NAME>` | 是 | 场次表 token；例如 `FEISHU_SHEET_BTS` 会生成场次名 `bts` |
| `FEISHU_SHEETS_JSON` | 否 | 单变量完整配置；设置后优先于 `FEISHU_SHEET_*` |
| `VIEWER_ADMIN_USER` | 否 | 管理员用户名，默认 `Admin` |
| `VIEWER_ADMIN_PASSWORD` | 否 | 管理员密码；不设置则管理员入口禁用 |

> 启动时 `mobile_app.py` 会自动读取同目录 `.env`（无第三方依赖），也可改用系统环境变量。

## 启动

```bash
./mobile_app.command
# 或
python3 mobile_app.py --host 0.0.0.0 --port 8765
```

启动后访问 `http://localhost:8765`。需公网访问可用 `cloudflared tunnel --url http://localhost:8765`。
