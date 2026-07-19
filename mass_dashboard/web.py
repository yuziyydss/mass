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


def build_handler(config: AppConfig, scheduler: DashboardScheduler):
    template_dir = Path(__file__).resolve().parent / "templates"
    env = Environment(
        loader=FileSystemLoader(template_dir),
        autoescape=select_autoescape(["html", "xml"]),
    )

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
                elif path == "/api/summary":
                    self._send_json(storage.get_summary(config.db_path, qs.get("date", [None])[0]))
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
