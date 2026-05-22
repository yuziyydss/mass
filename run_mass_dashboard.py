#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import logging

import mass_t

from mass_dashboard import bars as bar_cache
from mass_dashboard import storage
from mass_dashboard.config import AppConfig
from mass_dashboard.pipeline import run_mass_pipeline
from mass_dashboard.scheduler import DashboardScheduler
from mass_dashboard.web import run_server


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MASS daily factor job and web dashboard")
    parser.add_argument("--env-file", default=".env", help="Path to .env file")
    parser.add_argument("--host", default=None, help="Web bind host")
    parser.add_argument("--port", type=int, default=None, help="Web bind port")
    parser.add_argument("--run-time", default=None, help="Daily run time, HH:MM")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")

    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="Start dashboard web server and daily scheduler")
    serve.add_argument("--no-auto-import", action="store_true", help="Do not import historical mass_*.csv on first startup")

    run = sub.add_parser("run", help="Run MASS job immediately")
    run.add_argument("--date", default=None, help="Trade date in YYYYMMDD; default is latest trading day")
    run.add_argument("--force", action="store_true", help="Re-run even if this date already succeeded")

    cache = sub.add_parser("cache-bars", help="Preload local daily bar cache without calculating MASS")
    cache.add_argument("--date", default=None, help="Cache window ending at this trade date; default is latest trading day")

    sub.add_parser("import", help="Import existing factors/mass_*.csv into SQLite")

    return parser.parse_args()


def require_token(config: AppConfig) -> str:
    if not config.tushare_token:
        raise RuntimeError("Missing Tushare token. Please check TUSHARE_TOKEN/TS_TOKEN in .env")
    return config.tushare_token


def main() -> int:
    args = parse_args()
    setup_logging(args.verbose)
    command = args.command or "serve"
    config = AppConfig.load(
        env_file=args.env_file,
        host=args.host,
        port=args.port,
        run_time=args.run_time,
    )
    storage.init_db(config.db_path)
    stale_jobs = storage.mark_stale_running_jobs(config.db_path)
    if stale_jobs:
        logging.warning("Marked stale RUNNING jobs as FAILED: %s", stale_jobs)

    if command == "import":
        imported = storage.import_mass_csvs(config.db_path, config.exports_dir)
        logging.info("Imported historical MASS CSV rows: %s", imported)
        return 0

    if command == "cache-bars":
        pro = mass_t.init_tushare_client(require_token(config))
        resolved_date = mass_t.resolve_trade_date(pro, args.date)
        cfg = mass_t.RuntimeConfig()
        stock_list = mass_t.load_stock_list(pro)
        info = bar_cache.ensure_daily_bar_cache(
            pro=pro,
            db_path=config.db_path,
            end_date=resolved_date,
            cfg=cfg,
            expected_stock_count=len(stock_list),
        )
        logging.info(
            "Daily bar cache ready: %s to %s, fetched_dates=%s, fetched_rows=%s",
            info["start_date"],
            info["end_date"],
            len(info["missing_dates"]),
            info["fetched_rows"],
        )
        return 0

    if command == "run":
        result = run_mass_pipeline(config, trade_date=args.date, force=args.force)
        logging.info("Job completed: %s", result)
        return 0

    if command == "serve":
        if not args.no_auto_import and not storage.latest_trade_date(config.db_path):
            imported = storage.import_mass_csvs(config.db_path, config.exports_dir)
            logging.info("First startup auto-imported historical MASS CSV rows: %s", imported)

        scheduler = DashboardScheduler(config)
        scheduler.start()
        run_server(config, scheduler)
        return 0

    raise SystemExit(f"Unknown command: {command}")


if __name__ == "__main__":
    raise SystemExit(main())
