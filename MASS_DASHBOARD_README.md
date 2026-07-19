# MASS Dashboard

这是一个面向服务器部署的 MASS 因子后台，包含每日调度、SQLite 落库、历史 CSV 导入、网页可视化、任务日志、手动补跑、原始行情缓存和高盛关注模块。

## 本地运行

```powershell
python run_mass_dashboard.py import
python run_mass_dashboard.py --port 8018 serve --no-auto-import
```

打开：

```text
http://127.0.0.1:8018
```

## 立即运行一次 MASS

```powershell
python run_mass_dashboard.py run --date 20260321
```

不传 `--date` 时会自动取最近交易日。任务会先补齐 `daily_bars` 本地行情缓存，再从缓存计算 MASS；如果缓存结果为空，会回退到原来的逐股 Tushare 拉取逻辑。

## 预热本地行情缓存

如果你准备把服务放到服务器，建议先单独预热行情缓存：

```powershell
python run_mass_dashboard.py cache-bars --date 20260515
```

这个命令只下载并入库日行情，不计算 MASS。缓存表是 SQLite 里的 `daily_bars`，后续每日任务会复用它，减少重复 API 请求。

## 每日自动运行

服务启动后会按 `.env` 中的 `MASS_RUN_TIME` 每天触发一次。默认是 `18:30`，时区默认 `Asia/Shanghai`。

```env
TUSHARE_TOKEN=your_token
MASS_RUN_TIME=18:30
TIMEZONE=Asia/Shanghai
DASHBOARD_HOST=0.0.0.0
DASHBOARD_PORT=8008
APP_USERNAME=admin
APP_PASSWORD=change-me
MASS_QUALITY_MIN_ROWS=4000
MASS_ALERT_WEBHOOK_URL=
MASS_ALERT_WEBHOOK_TYPE=feishu
GOLDMAN_DATA_DIR=factors/goldman
```

如果 `APP_PASSWORD` 为空，则不启用登录保护。放到公网服务器时建议一定设置密码，并用 Nginx/HTTPS 反代。

## Docker 部署

```bash
docker compose -f docker-compose.dashboard.yml up -d --build
```

数据会保存在：

```text
dashboard_data/mass_dashboard.db
factors/
```

## 页面功能

- 最新交易日 MASS 摘要
- MASS 主数据表：每页 100 条、分页、日期筛选、行业筛选、代码/名称查询
- 点击个股进入详情页，查看按时间顺序排列的历史 MASS 数据
- 个股详情页展示 `mass_raw` 折线图
- 行业 MASS 均值统计
- 手动触发当天任务或补跑指定日期
- 查看最近任务日志与实时任务进度
- 高盛关注作为独立模块，展示 MASS 与高盛持仓交叉结果
- 任务成功、失败或质量异常时可通过 Webhook 告警

## 关键文件

```text
run_mass_dashboard.py               启动入口和 CLI
mass_dashboard/bars.py              日行情缓存与本地 MASS 计算
mass_dashboard/pipeline.py          MASS 任务编排（含 moneyflow + 底部条件阶段）
mass_dashboard/scheduler.py         每日调度（失败自动重试3次）
mass_dashboard/storage.py           SQLite 表结构、查询、schema 迁移
mass_dashboard/notifier.py          飞书/通用 Webhook 告警
mass_dashboard/web.py               HTTP API 和网页服务（27 个端点）
mass_dashboard/templates/index.html 网页首页（4 标签页 + 暗色模式）
mass_dashboard/templates/stock.html 个股详情页（K线+RSI+MACD+财务）
mass_dashboard/moneyflow.py         周K下跌+主力净流入
mass_dashboard/bottom.py            底部4条件筛选（地量/不创新低/估值低/底背离）
mass_dashboard/factor_analysis.py   因子IC/IR + 分层回测 + 多因子合成
mass_dashboard/backtest.py          回测引擎（选股+夏普+最大回撤）
mass_dashboard/momentum.py          动量/波动率/换手率因子
mass_dashboard/financial.py         财务指标按需拉取
mass_dashboard/quality.py           质量检查 + 数据时效监控
```

## 功能模块

- **4 标签页**：总览 / MASS 数据表 / 因子分析 / 选股筛选
- **因子分析**：IC/IR、分层回测、多因子对比、回测引擎、多因子合成
- **选股筛选**：周K下跌+净流入、底部4条件
- **个股分析**：K线+成交量+MA、RSI+MACD、财务指标、MASS历史
- **数据健康**：时效监控、schema 自动迁移
- **其他**：自选股收藏、CSV 导出、API 文档(/api-docs)、暗色模式

## 当前数据结构

- `factor_mass_daily`：每日 MASS 因子结果（含 pb/dv_ratio）
- `daily_bars`：原始日行情缓存，用于加速 MASS 计算和回测
- `daily_moneyflow`：资金流缓存，用于周K净流入
- `week_down_flow`：周K下跌+净流入结果
- `bottom_conditions`：底部4条件结果
- `watchlist`：自选股
- `job_runs`：任务运行记录
- `job_progress`：任务实时进度
- `factor_alerts`：质量告警

## 告警 Webhook

默认按飞书机器人格式发送：

```env
MASS_ALERT_WEBHOOK_TYPE=feishu
MASS_ALERT_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxxx
```

如果你想接自己的 HTTP 服务：

```env
MASS_ALERT_WEBHOOK_TYPE=generic
MASS_ALERT_WEBHOOK_URL=https://example.com/webhook
```

generic 模式会发送：

```json
{"title": "...", "text": "..."}
```
