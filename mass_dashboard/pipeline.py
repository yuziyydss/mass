#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

import mass_t

from .config import AppConfig
from .notifier import send_notification
from .quality import check_mass_quality
from . import bars as bar_cache
from . import storage

LOGGER = logging.getLogger("mass_dashboard.pipeline")
JOB_NAME = "mass_tushare_daily"


def run_mass_pipeline(config: AppConfig, trade_date: Optional[str] = None, force: bool = False) -> dict:
    if not config.tushare_token:
        raise RuntimeError("Missing Tushare token. Please check TUSHARE_TOKEN/TS_TOKEN in .env")

    storage.init_db(config.db_path)
    config.exports_dir.mkdir(parents=True, exist_ok=True)

    pro = mass_t.init_tushare_client(config.tushare_token)
    resolved_date = mass_t.resolve_trade_date(pro, trade_date)

    if storage.has_successful_run(config.db_path, JOB_NAME, resolved_date) and not force:
        run_id = storage.create_job_run(config.db_path, JOB_NAME, resolved_date, status="SKIPPED")
        message = "This trade date already has a successful run; skipped duplicate job"
        storage.update_job_progress(config.db_path, run_id, JOB_NAME, resolved_date, "SKIPPED", message=message)
        storage.finish_job_run(config.db_path, run_id, "SKIPPED", 0, message)
        return {"status": "SKIPPED", "trade_date": resolved_date, "row_count": 0}

    run_id = storage.create_job_run(config.db_path, JOB_NAME, resolved_date)
    try:
        cfg = mass_t.RuntimeConfig()
        out_file = config.exports_dir / f"mass_{resolved_date}.csv"
        progress_file = config.exports_dir / f"progress_{resolved_date}.pkl"
        cache_file = config.exports_dir / "industry_cache.pkl"

        storage.update_job_progress(config.db_path, run_id, JOB_NAME, resolved_date, "LOAD_STOCKS", message="Loading stock list")
        LOGGER.info("Starting MASS pipeline: %s", resolved_date)
        stock_list = mass_t.load_stock_list(pro)

        storage.update_job_progress(config.db_path, run_id, JOB_NAME, resolved_date, "LOAD_MARKET_CAP", message="Loading market cap data")
        mkt_cap = mass_t.load_market_cap(pro, resolved_date, cfg)

        stock_codes = stock_list["code"].tolist()
        storage.update_job_progress(config.db_path, run_id, JOB_NAME, resolved_date, "LOAD_INDUSTRY", message="Loading industry mapping")
        industry_map = mass_t.load_wind_industry_map(pro, stock_codes, cache_file, cfg)
        if industry_map.empty:
            industry_map = pd.DataFrame(columns=["code", "industry"])

        base = stock_list.merge(industry_map, on="code", how="left")
        base = base.merge(mkt_cap, on="code", how="left")

        def on_progress(processed: int, total: int, current_code: str, row_count: int) -> None:
            storage.update_job_progress(
                config.db_path,
                run_id,
                JOB_NAME,
                resolved_date,
                "CALCULATING",
                processed=processed,
                total=total,
                current_code=current_code,
                message=f"Generated {row_count} rows",
            )

        def on_cache_progress(processed: int, total: int, current_date: str, message: str) -> None:
            storage.update_job_progress(
                config.db_path,
                run_id,
                JOB_NAME,
                resolved_date,
                "CACHE_BARS",
                processed=processed,
                total=total,
                current_code=current_date,
                message=message,
            )

        storage.update_job_progress(
            config.db_path,
            run_id,
            JOB_NAME,
            resolved_date,
            "CACHE_BARS",
            total=len(base),
            message="Ensuring local daily bar cache",
        )
        cache_info = bar_cache.ensure_daily_bar_cache(
            pro=pro,
            db_path=config.db_path,
            end_date=resolved_date,
            cfg=cfg,
            expected_stock_count=len(base),
            progress_callback=on_cache_progress,
        )

        storage.update_job_progress(
            config.db_path,
            run_id,
            JOB_NAME,
            resolved_date,
            "CALCULATING",
            total=len(base),
            message="Calculating MASS from local daily bar cache",
        )
        rows = bar_cache.calculate_mass_from_cache(
            db_path=config.db_path,
            base=base,
            trade_date=resolved_date,
            trade_dates=cache_info["trade_dates"],
            cfg=cfg,
            progress_callback=on_progress,
        )

        storage.update_job_progress(config.db_path, run_id, JOB_NAME, resolved_date, "POST_PROCESS", message="Post-processing factor values")
        final_df = mass_t.post_process(rows, base)
        if final_df.empty:
            LOGGER.warning("Local cache calculation is empty; falling back to per-stock Tushare fetch")
            existing_codes, existing_rows = mass_t.load_resume_state(out_file, progress_file, base)
            base_to_process = base[~base["code"].isin(existing_codes)].copy()
            storage.update_job_progress(
                config.db_path,
                run_id,
                JOB_NAME,
                resolved_date,
                "CALCULATING",
                processed=len(existing_codes),
                total=len(base),
                message="Cache calculation empty; falling back to per-stock calculation",
            )
            rows = mass_t.run_mass_loop(
                pro=pro,
                base_to_process=base_to_process,
                trade_date=resolved_date,
                existing_rows=existing_rows,
                existing_count=len(existing_codes),
                progress_file=progress_file,
                cfg=cfg,
                progress_callback=on_progress,
            )
            final_df = mass_t.post_process(rows, base)

        if final_df.empty:
            raise RuntimeError("MASS result is empty")

        storage.update_job_progress(config.db_path, run_id, JOB_NAME, resolved_date, "SAVE", message="Saving MASS results")
        mass_t.save_final_result(final_df, out_file, progress_file)
        row_count = storage.upsert_mass_results(config.db_path, final_df, resolved_date)
        alerts = check_mass_quality(final_df, config.quality_min_rows)
        storage.replace_alerts(config.db_path, resolved_date, alerts)
        storage.finish_job_run(config.db_path, run_id, "SUCCESS", row_count)
        storage.update_job_progress(
            config.db_path,
            run_id,
            JOB_NAME,
            resolved_date,
            "SUCCESS",
            processed=row_count,
            total=row_count,
            message=f"Finished, saved {row_count} rows",
        )
        if alerts:
            send_notification(
                config,
                f"MASS job completed with alerts: {resolved_date}",
                "\n".join([f"[{level}] {message}" for level, message in alerts]),
            )
        else:
            send_notification(config, f"MASS job succeeded: {resolved_date}", f"Saved {row_count} rows")
        LOGGER.info("MASS pipeline finished: %s rows=%s", resolved_date, row_count)
        return {"status": "SUCCESS", "trade_date": resolved_date, "row_count": row_count, "alerts": alerts}
    except Exception as err:
        storage.finish_job_run(config.db_path, run_id, "FAILED", 0, str(err))
        storage.update_job_progress(config.db_path, run_id, JOB_NAME, resolved_date, "FAILED", message=str(err))
        send_notification(config, f"MASS job failed: {resolved_date}", str(err))
        LOGGER.exception("MASS pipeline failed: %s", resolved_date)
        raise
