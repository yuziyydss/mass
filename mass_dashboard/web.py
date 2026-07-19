#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import base64
import hmac
import json
import logging
import math
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import storage
from .config import AppConfig
from .scheduler import DashboardScheduler
from . import factor_analysis
from . import backtest
from . import momentum
from . import financial
import mass_t

LOGGER = logging.getLogger("mass_dashboard.web")


def json_default(value):
    if isinstance(value, (np.integer, np.floating)):
        return value.item()
    return str(value)


def json_safe(value):
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, np.generic):
        return json_safe(value.item())
    return value


def _analyze_factor(config: AppConfig, qs: dict) -> dict:
    """统一因子分析入口，支持 MASS / 动量 / 波动率 因子。"""
    factor = qs.get("factor", ["mass_zscore"])[0]
    if factor.startswith("momentum"):
        parts = factor.split("_")
        period = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 20
        factor_panel = momentum.compute_momentum_panel(config.db_path, period)
    elif factor.startswith("volatility"):
        parts = factor.split("_")
        period = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 20
        factor_panel = momentum.compute_volatility_panel(config.db_path, period)
    elif factor.startswith("turnover"):
        parts = factor.split("_")
        period = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 20
        factor_panel = momentum.compute_turnover_panel(config.db_path, period)
    else:
        return factor_analysis.analyze_factor(config.db_path, factor_col=factor)
    if factor_panel.empty:
        return {"error": f"因子({factor})面板为空"}
    dates = factor_panel.index.tolist()
    close_panel = storage.load_close_panel(config.db_path, dates[0], dates[-1])
    common = factor_panel.index.intersection(close_panel.index).tolist()
    return factor_analysis.analyze_factor_from_panels(
        factor_panel.loc[common], close_panel.loc[common],
        forward_days_list=[5, 10, 20],
    )


def build_handler(config: AppConfig, scheduler: DashboardScheduler):
    template_dir = Path(__file__).resolve().parent / "templates"
    env = Environment(
        loader=FileSystemLoader(template_dir),
        autoescape=select_autoescape(["html", "xml"]),
    )
    # 按需初始化 tushare client（财务指标等按股拉取用）
    _pro = mass_t.init_tushare_client(config.tushare_token) if config.tushare_token else None

    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "MassDashboard/0.1"

        def log_message(self, fmt, *args):
            LOGGER.info("%s - %s", self.address_string(), fmt % args)

        def _is_authorized(self) -> bool:
            if not config.app_password:
                return True
            header = self.headers.get("Authorization", "")
            if not header.startswith("Basic "):
                return False
            try:
                decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
            except Exception:
                return False
            username, _, password = decoded.partition(":")
            # 常量时间比较，避免时序侧信道
            return (
                username == config.app_username
                and hmac.compare_digest(password, config.app_password)
            )

        def _require_auth(self) -> bool:
            if self._is_authorized():
                return True
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="MASS Dashboard"')
            self.end_headers()
            return False

        def _send_json(self, payload, status: int = 200) -> None:
            body = json.dumps(
                json_safe(payload),
                ensure_ascii=False,
                default=json_default,
                allow_nan=False,
            ).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, html: str) -> None:
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self._require_auth():
                return

            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            path = parsed.path
            try:
                if path == "/":
                    latest_date = storage.latest_trade_date(config.db_path)
                    template = env.get_template("index.html")
                    self._send_html(
                        template.render(
                            latest_date=latest_date or "",
                            run_time=config.run_time,
                            timezone=config.timezone,
                        )
                    )
                elif path == "/stock":
                    code = qs.get("code", [""])[0]
                    profile = storage.stock_profile(config.db_path, code) if code else None
                    template = env.get_template("stock.html")
                    self._send_html(template.render(code=code, profile=profile or {}))
                elif path == "/api-docs":
                    self._send_html("""<!doctype html><html><head><meta charset=utf-8><title>MASS API 文档</title>
<style>body{font-family:Segoe UI,sans-serif;max-width:900px;margin:30px auto;padding:0 20px;color:#17201b}
h1{color:#0d7c66}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ddd;padding:6px 8px;text-align:left;font-size:13px}
code{background:#f0f4f0;padding:2px 5px;border-radius:3px;color:#0d7c66}.m{color:#6b746f}</style></head>
<body><h1>◈ MASS Dashboard API 文档</h1>
<p class=m>所有端点需要 Basic Auth (admin/admin123)。日期格式 YYYYMMDD。</p>
<table><tr><th>方法</th><th>路径</th><th>参数</th><th>说明</th></tr>
<tr><td>GET</td><td><code>/</code></td><td>-</td><td>首页 dashboard</td></tr>
<tr><td>GET</td><td><code>/stock</code></td><td>code</td><td>个股详情页</td></tr>
<tr><td>GET</td><td><code>/api/summary</code></td><td>date?</td><td>交易日摘要</td></tr>
<tr><td>GET</td><td><code>/api/health</code></td><td>-</td><td>数据健康检查</td></tr>
<tr><td>GET</td><td><code>/api/dates</code></td><td>-</td><td>所有交易日</td></tr>
<tr><td>GET</td><td><code>/api/industries</code></td><td>date?</td><td>行业列表</td></tr>
<tr><td>GET</td><td><code>/api/mass</code></td><td>date?,page,per_page,industry,q,direction</td><td>MASS分页</td></tr>
<tr><td>GET</td><td><code>/api/latest</code></td><td>date?,limit,industry,q,direction</td><td>MASS最新</td></tr>
<tr><td>GET</td><td><code>/api/industry</code></td><td>date?</td><td>行业统计</td></tr>
<tr><td>GET</td><td><code>/api/industry-rotation</code></td><td>-</td><td>行业轮动</td></tr>
<tr><td>GET</td><td><code>/api/history</code></td><td>code</td><td>个股MASS历史</td></tr>
<tr><td>GET</td><td><code>/api/kline</code></td><td>code,limit?</td><td>个股K线+MA</td></tr>
<tr><td>GET</td><td><code>/api/financial</code></td><td>code</td><td>财务指标</td></tr>
<tr><td>GET</td><td><code>/api/focus</code></td><td>date?,limit</td><td>高盛关注</td></tr>
<tr><td>GET</td><td><code>/api/week-flow</code></td><td>date?,limit</td><td>周K净流入</td></tr>
<tr><td>GET</td><td><code>/api/bottom</code></td><td>date?,min,limit</td><td>底部4条件</td></tr>
<tr><td>GET</td><td><code>/api/factor-ic</code></td><td>factor</td><td>因子IC/IR</td></tr>
<tr><td>GET</td><td><code>/api/factor-quantile</code></td><td>factor</td><td>分层回测</td></tr>
<tr><td>GET</td><td><code>/api/factor-compare</code></td><td>-</td><td>多因子对比</td></tr>
<tr><td>GET</td><td><code>/api/backtest</code></td><td>factor,n,hold,dir</td><td>回测</td></tr>
<tr><td>GET</td><td><code>/api/watchlist</code></td><td>action?</td><td>自选股</td></tr>
<tr><td>GET</td><td><code>/api/export</code></td><td>同/api/mass</td><td>导出CSV</td></tr>
<tr><td>GET</td><td><code>/api/jobs</code></td><td>-</td><td>任务历史</td></tr>
<tr><td>GET</td><td><code>/api/progress</code></td><td>-</td><td>实时进度</td></tr>
<tr><td>GET</td><td><code>/api/alerts</code></td><td>-</td><td>告警</td></tr>
<tr><td>POST</td><td><code>/api/run</code></td><td>date?,force?</td><td>触发任务</td></tr>
<tr><td>POST</td><td><code>/api/import</code></td><td>-</td><td>导入CSV</td></tr>
<tr><td>POST</td><td><code>/api/watchlist</code></td><td>code,name?</td><td>加自选</td></tr>
</table><p class=m>因子: mass_zscore/mass_neu/mass_raw/momentum_5/momentum_20/momentum_60</p>
</body></html>""")
                elif path == "/api/summary":
                    self._send_json(storage.get_summary(config.db_path, qs.get("date", [None])[0]))
                elif path == "/api/health":
                    from .quality import check_data_freshness
                    fresh = check_data_freshness(config.db_path)
                    summary = storage.get_summary(config.db_path)
                    latest = summary.get("trade_date")
                    self._send_json({
                        "latest_date": latest,
                        "row_count": summary.get("row_count", 0),
                        "alert": {"level": fresh[0], "message": fresh[1]} if fresh else None,
                        "healthy": fresh is None,
                    })
                elif path == "/api/dates":
                    self._send_json({"dates": storage.list_trade_dates(config.db_path)})
                elif path == "/api/industries":
                    self._send_json(
                        {
                            "industries": storage.list_industries(
                                config.db_path,
                                trade_date=qs.get("date", [None])[0],
                            )
                        }
                    )
                elif path == "/api/mass":
                    self._send_json(
                        storage.query_mass_page(
                            config.db_path,
                            trade_date=qs.get("date", [None])[0],
                            page=int(qs.get("page", ["1"])[0]),
                            per_page=int(qs.get("per_page", ["100"])[0]),
                            industry=qs.get("industry", [""])[0],
                            keyword=qs.get("q", [""])[0],
                            direction=qs.get("direction", ["desc"])[0],
                        )
                    )
                elif path == "/api/latest":
                    self._send_json(
                        {
                            "rows": storage.query_mass(
                                config.db_path,
                                trade_date=qs.get("date", [None])[0],
                                limit=int(qs.get("limit", ["100"])[0]),
                                industry=qs.get("industry", [""])[0],
                                keyword=qs.get("q", [""])[0],
                                direction=qs.get("direction", ["desc"])[0],
                            )
                        }
                    )
                elif path == "/api/industry":
                    self._send_json({"rows": storage.industry_stats(config.db_path, qs.get("date", [None])[0])})
                elif path == "/api/history":
                    code = qs.get("code", [""])[0]
                    self._send_json({"rows": storage.stock_history(config.db_path, code)})
                elif path == "/api/kline":
                    code = qs.get("code", [""])[0]
                    limit = int(qs.get("limit", ["250"])[0])
                    self._send_json({"rows": storage.load_kline(config.db_path, code, limit)})
                elif path == "/api/financial":
                    code = qs.get("code", [""])[0]
                    if not code or not _pro:
                        self._send_json({"error": "需要 code 和 tushare token"})
                    else:
                        self._send_json(financial.fetch_financial(_pro, code))
                elif path == "/api/export":
                    import csv, io
                    rows = storage.query_mass(
                        config.db_path,
                        trade_date=qs.get("date", [None])[0],
                        limit=int(qs.get("limit", ["5000"])[0]),
                        industry=qs.get("industry", [""])[0],
                        keyword=qs.get("q", [""])[0],
                        direction=qs.get("direction", ["desc"])[0],
                    )
                    buf = io.StringIO()
                    writer = csv.writer(buf)
                    cols = ["trade_date","code","name","industry","total_mkt_cap","pe","pb","dv_ratio","mass_raw","mass_neu","mass_zscore"]
                    writer.writerow(cols)
                    for r in rows:
                        writer.writerow([r.get(c, "") for c in cols])
                    body = buf.getvalue().encode("utf-8-sig")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/csv; charset=utf-8")
                    self.send_header("Content-Disposition", "attachment; filename=mass_export.csv")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif path == "/api/compare":
                    codes = qs.get("codes", [""])[0].split(",")
                    codes = [c.strip() for c in codes if c.strip()]
                    self._send_json(storage.compare_stocks_zscore(config.db_path, codes))
                elif path == "/api/correlation":
                    codes = qs.get("codes", [""])[0].split(",")
                    codes = [c.strip() for c in codes if c.strip()]
                    self._send_json(storage.correlation_matrix(config.db_path, codes))
                elif path == "/api/industry-rotation":
                    rows = storage.industry_rotation(config.db_path, limit_dates=10)
                    self._send_json({"rows": rows})
                elif path == "/api/watchlist":
                    action = qs.get("action", ["get"])[0]
                    if action == "delete":
                        code = qs.get("code", [""])[0]
                        self._send_json({"ok": storage.remove_from_watchlist(config.db_path, code)})
                    else:
                        self._send_json({"rows": storage.list_watchlist(config.db_path)})
                elif path == "/api/jobs":
                    self._send_json({"rows": storage.recent_jobs(config.db_path)})
                elif path == "/api/progress":
                    self._send_json({"progress": storage.latest_progress(config.db_path)})
                elif path == "/api/alerts":
                    self._send_json({"rows": storage.recent_alerts(config.db_path)})
                elif path == "/api/focus":
                    self._send_json(
                        storage.focus_with_goldman(
                            config.db_path,
                            config.goldman_dir,
                            trade_date=qs.get("date", [None])[0],
                            limit=int(qs.get("limit", ["100"])[0]),
                        )
                    )
                elif path == "/api/week-flow":
                    self._send_json(
                        storage.query_week_down_flow(
                            config.db_path,
                            trade_date=qs.get("date", [None])[0],
                            limit=int(qs.get("limit", ["100"])[0]),
                        )
                    )
                elif path == "/api/bottom":
                    self._send_json(
                        storage.query_bottom_conditions(
                            config.db_path,
                            trade_date=qs.get("date", [None])[0],
                            min_conditions=int(qs.get("min", ["2"])[0]),
                            limit=int(qs.get("limit", ["100"])[0]),
                        )
                    )
                elif path == "/api/factor-ic":
                    self._send_json(self._analyze_factor(config, qs))
                elif path == "/api/factor-quantile":
                    self._send_json(self._analyze_factor(config, qs))
                elif path == "/api/factor-compare":
                    specs = [
                        {"name": "mass_zscore"}, {"name": "mass_neu"}, {"name": "mass_raw"},
                        {"name": "momentum_5"}, {"name": "momentum_20"}, {"name": "momentum_60"},
                        {"name": "volatility_20"},
                    ]
                    self._send_json({"rows": factor_analysis.compare_factors(config.db_path, specs)})
                elif path == "/api/factor-distribution":
                    self._send_json(factor_analysis.factor_distribution(config.db_path, factor_col=qs.get("factor", ["mass_zscore"])[0]))
                elif path == "/api/factor-synth":
                    # 合成因子: momentum_5(正) + volatility_20(负) + mass_zscore(正)
                    panel = factor_analysis.synthesize_factor(
                        config.db_path,
                        ["momentum_5", "volatility_20", "mass_zscore"],
                        [1.0, -1.0, 0.5],
                    )
                    if panel.empty:
                        self._send_json({"error": "合成因子面板为空"})
                        return
                    dates = panel.index.tolist()
                    close_panel = storage.load_close_panel(config.db_path, dates[0], dates[-1])
                    common = panel.index.intersection(close_panel.index).tolist()
                    self._send_json(factor_analysis.analyze_factor_from_panels(
                        panel.loc[common], close_panel.loc[common], [5, 10, 20]
                    ))
                elif path == "/api/backtest":
                    self._send_json(
                        backtest.run_backtest(
                            config.db_path,
                            factor_col=qs.get("factor", ["mass_zscore"])[0],
                            top_n=int(qs.get("n", ["50"])[0]),
                            hold_days=int(qs.get("hold", ["5"])[0]),
                            direction=qs.get("dir", ["top"])[0],
                        )
                    )
                else:
                    self._send_json({"error": "not found"}, status=404)
            except Exception as err:
                LOGGER.exception("GET %s failed", path)
                self._send_json({"error": str(err)}, status=500)

        def do_POST(self):
            if not self._require_auth():
                return

            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            try:
                if parsed.path == "/api/run":
                    trade_date = qs.get("date", [None])[0] or None
                    force = qs.get("force", ["0"])[0] in {"1", "true", "True"}
                    ok, message = scheduler.trigger_run(trade_date=trade_date, force=force)
                    self._send_json({"ok": ok, "message": message})
                elif parsed.path == "/api/import":
                    count = storage.import_mass_csvs(config.db_path, config.exports_dir)
                    self._send_json({"ok": True, "imported_rows": count})
                elif parsed.path == "/api/watchlist":
                    code = qs.get("code", [""])[0]
                    name = qs.get("name", [""])[0]
                    if not code:
                        self._send_json({"error": "需要 code"}, status=400)
                    else:
                        storage.add_to_watchlist(config.db_path, code, name=name)
                        self._send_json({"ok": True})
                else:
                    self._send_json({"error": "not found"}, status=404)
            except Exception as err:
                LOGGER.exception("POST %s failed", parsed.path)
                self._send_json({"error": str(err)}, status=500)

    return DashboardHandler


def run_server(config: AppConfig, scheduler: DashboardScheduler) -> None:
    storage.init_db(config.db_path)
    handler = build_handler(config, scheduler)
    server = ThreadingHTTPServer((config.host, config.port), handler)
    LOGGER.info("MASS Dashboard running at http://%s:%s", config.host, config.port)
    try:
        server.serve_forever()
    finally:
        scheduler.stop()
        server.server_close()
