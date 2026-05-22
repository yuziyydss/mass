#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional, Sequence

import pandas as pd

import mass_t

from . import storage

LOGGER = logging.getLogger("mass_dashboard.bars")
DAILY_FIELDS = "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount"


def trade_dates_for_window(pro, end_date: str, window_days: int) -> list[str]:
    end_dt = datetime.strptime(end_date, "%Y%m%d")
    start_dt = end_dt - timedelta(days=window_days)
    start_date = start_dt.strftime("%Y%m%d")

    try:
        cal = pro.trade_cal(exchange="SSE", start_date=start_date, end_date=end_date)
        if cal is not None and not cal.empty and {"cal_date", "is_open"}.issubset(cal.columns):
            dates = cal[cal["is_open"] == 1]["cal_date"].astype(str).sort_values().tolist()
            if dates:
                return dates
    except Exception as err:
        LOGGER.warning("读取交易日历失败，使用自然日回退: %s", err)

    dates = []
    current = start_dt
    while current <= end_dt:
        dates.append(current.strftime("%Y%m%d"))
        current += timedelta(days=1)
    return dates


def fetch_daily_bars(pro, trade_date: str, cfg: mass_t.RuntimeConfig) -> pd.DataFrame:
    for attempt in range(cfg.max_retries):
        try:
            data = pro.daily(trade_date=trade_date, fields=DAILY_FIELDS)
            if data is None:
                return pd.DataFrame(columns=storage.DAILY_BAR_COLUMNS)
            if data.empty:
                LOGGER.warning("trade_date=%s 行情为空", trade_date)
                return pd.DataFrame(columns=storage.DAILY_BAR_COLUMNS)
            return data.rename(columns={"ts_code": "code"})
        except Exception as err:
            LOGGER.warning(
                "批量读取日行情失败(%s/%s, trade_date=%s): %s",
                attempt + 1,
                cfg.max_retries,
                trade_date,
                err,
            )
            if attempt < cfg.max_retries - 1:
                time.sleep(cfg.fetch_retry_sleep_seconds)

    return pd.DataFrame(columns=storage.DAILY_BAR_COLUMNS)


def ensure_daily_bar_cache(
    pro,
    db_path: Path,
    end_date: str,
    cfg: mass_t.RuntimeConfig,
    expected_stock_count: int,
    progress_callback: Optional[Callable[[int, int, str, str], None]] = None,
) -> dict:
    trade_dates = trade_dates_for_window(pro, end_date, cfg.history_window_days)
    if not trade_dates:
        return {
            "start_date": end_date,
            "end_date": end_date,
            "trade_dates": [],
            "missing_dates": [],
            "fetched_rows": 0,
            "min_rows": 0,
        }

    if expected_stock_count <= 0:
        min_rows = 1
    elif expected_stock_count < 1500:
        min_rows = max(1, int(expected_stock_count * 0.6))
    else:
        min_rows = max(1000, int(expected_stock_count * 0.6))
    missing_dates = storage.missing_daily_bar_dates(db_path, trade_dates, min_rows=min_rows)
    fetched_rows = 0
    total = len(missing_dates)

    if progress_callback:
        progress_callback(0, total, "", f"本地缺少 {total} 个交易日行情，开始补缓存")

    for index, trade_date in enumerate(missing_dates, start=1):
        data = fetch_daily_bars(pro, trade_date, cfg)
        inserted = storage.upsert_daily_bars(db_path, data)
        fetched_rows += inserted
        LOGGER.info("行情缓存 %s: upsert %s rows", trade_date, inserted)
        if progress_callback:
            progress_callback(index, total, trade_date, f"已缓存 {inserted} 行行情")
        if index < total:
            time.sleep(cfg.request_sleep_seconds)

    return {
        "start_date": trade_dates[0],
        "end_date": trade_dates[-1],
        "trade_dates": trade_dates,
        "missing_dates": missing_dates,
        "fetched_rows": fetched_rows,
        "min_rows": min_rows,
    }


def calculate_mass_from_cache(
    db_path: Path,
    base: pd.DataFrame,
    trade_date: str,
    trade_dates: Sequence[str],
    cfg: mass_t.RuntimeConfig,
    progress_callback: Optional[Callable[[int, int, str, int], None]] = None,
) -> list[dict]:
    if base.empty or not trade_dates:
        return []

    codes = base["code"].astype(str).tolist()
    bars = storage.load_daily_bars(db_path, min(trade_dates), trade_date, codes=codes)
    if bars.empty:
        LOGGER.warning("本地行情缓存为空，无法计算 MASS")
        return []

    bars["trade_date"] = bars["trade_date"].astype(str)
    bars["high"] = pd.to_numeric(bars["high"], errors="coerce")
    bars["low"] = pd.to_numeric(bars["low"], errors="coerce")
    bars = bars.dropna(subset=["code", "trade_date", "high", "low"])
    bars = bars.sort_values(["code", "trade_date"])
    groups = {code: hist for code, hist in bars.groupby("code", sort=False)}

    rows: list[dict] = []
    total = len(base)
    for index, row in enumerate(base.itertuples(index=False), start=1):
        code = str(row.code)
        hist = groups.get(code)
        val = mass_t.calc_mass_factor(hist) if hist is not None else None
        rows.append(
            {
                "code": code,
                "name": getattr(row, "name", None),
                "industry": getattr(row, "industry", None),
                "total_mkt_cap": getattr(row, "total_mkt_cap", None),
                "pe": getattr(row, "pe", None),
                "mass_raw": val,
            }
        )
        if progress_callback and index % cfg.progress_save_every == 0:
            progress_callback(index, total, code, len(rows))

    if progress_callback:
        progress_callback(total, total, "", len(rows))
    return rows
