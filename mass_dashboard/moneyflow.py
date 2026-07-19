#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional, Sequence

import numpy as np
import pandas as pd

import mass_t

from . import storage
from .bars import trade_dates_for_window

LOGGER = logging.getLogger("mass_dashboard.moneyflow")
MONEYFLOW_FIELDS = (
    "ts_code,trade_date,buy_sm_vol,sell_sm_vol,buy_md_vol,sell_md_vol,"
    "buy_lg_vol,sell_lg_vol,buy_elg_vol,sell_elg_vol,net_mf_vol,net_mf_amount"
)


def fetch_moneyflow(pro, trade_date: str, cfg: mass_t.RuntimeConfig) -> pd.DataFrame:
    """按交易日批量拉取全市场资金流数据。"""
    for attempt in range(cfg.max_retries):
        try:
            data = pro.moneyflow(trade_date=trade_date, fields=MONEYFLOW_FIELDS)
            if data is None:
                return pd.DataFrame(columns=storage.MONEYFLOW_COLUMNS)
            if data.empty:
                LOGGER.warning("trade_date=%s 资金流为空", trade_date)
                return pd.DataFrame(columns=storage.MONEYFLOW_COLUMNS)
            return data.rename(columns={"ts_code": "code"})
        except Exception as err:
            LOGGER.warning(
                "批量读取资金流失败(%s/%s, trade_date=%s): %s",
                attempt + 1, cfg.max_retries, trade_date, err,
            )
            if attempt < cfg.max_retries - 1:
                time.sleep(cfg.fetch_retry_sleep_seconds)
    return pd.DataFrame(columns=storage.MONEYFLOW_COLUMNS)


def ensure_moneyflow_cache(
    pro,
    db_path: Path,
    week_start: str,
    week_end: str,
    cfg: mass_t.RuntimeConfig,
    expected_stock_count: int,
    progress_callback: Optional[Callable[[int, int, str, str], None]] = None,
) -> dict:
    """缓存指定周范围内的资金流数据到 SQLite。"""
    trade_dates = trade_dates_for_window(pro, week_end, 10)
    # 只取本周内的交易日
    week_dates = [d for d in trade_dates if week_start <= d <= week_end]
    if not week_dates:
        return {"week_dates": [], "missing_dates": [], "fetched_rows": 0}

    min_rows = max(1, int(expected_stock_count * 0.5)) if expected_stock_count > 0 else 1
    missing = storage.missing_moneyflow_dates(db_path, week_dates, min_rows=min_rows)
    fetched_rows = 0
    total = len(missing)

    if progress_callback:
        progress_callback(0, total, "", f"缺少 {total} 个交易日资金流，开始补缓存")

    for index, trade_date in enumerate(missing, start=1):
        data = fetch_moneyflow(pro, trade_date, cfg)
        inserted = storage.upsert_moneyflow(db_path, data)
        fetched_rows += inserted
        LOGGER.info("资金流缓存 %s: upsert %s rows", trade_date, inserted)
        if progress_callback:
            progress_callback(index, total, trade_date, f"已缓存 {inserted} 行资金流")
        if index < total:
            time.sleep(cfg.request_sleep_seconds)

    return {"week_dates": week_dates, "missing_dates": missing, "fetched_rows": fetched_rows}


def _get_week_bounds(trade_date: str) -> tuple[str, str]:
    """给定当前交易日，返回本周（周一到周五）的起止日期字符串。"""
    dt = datetime.strptime(trade_date, "%Y%m%d")
    weekday = dt.weekday()
    monday = dt - timedelta(days=weekday)
    friday = monday + timedelta(days=4)
    return monday.strftime("%Y%m%d"), friday.strftime("%Y%m%d")


def _get_prev_week_bounds(week_start: str) -> tuple[str, str]:
    """给定本周起始日，返回上周周一到周五。"""
    dt = datetime.strptime(week_start, "%Y%m%d")
    prev_monday = dt - timedelta(days=7)
    prev_friday = prev_monday + timedelta(days=4)
    return prev_monday.strftime("%Y%m%d"), prev_friday.strftime("%Y%m%d")


def calculate_week_down_flow(
    db_path: Path,
    base: pd.DataFrame,
    trade_date: str,
    cfg: mass_t.RuntimeConfig,
    progress_callback: Optional[Callable[[int, int, str, int], None]] = None,
) -> list[dict]:
    """从本地缓存计算"周K下跌 + 主力净流入"的股票列表。

    逻辑：
    1. 取本周和上周的 close 数据，按股算周均价涨跌幅
    2. 取本周的主力净流入（net_mf_amount）
    3. 筛选：周均价下跌 + 主力净流入 > 0
    """
    if base.empty:
        return []

    week_start, week_end = _get_week_bounds(trade_date)
    prev_week_start, prev_week_end = _get_prev_week_bounds(week_start)

    codes = base["code"].astype(str).tolist()

    # 从 daily_bars 取本周 + 上周的 close
    bars = storage.load_daily_bars(
        db_path, prev_week_start, week_end, codes=codes,
        columns=["code", "trade_date", "close"],
    )
    if bars.empty:
        LOGGER.warning("本地行情缓存为空，无法计算周K下跌净流入")
        return []

    bars["trade_date"] = bars["trade_date"].astype(str)
    bars["close"] = pd.to_numeric(bars["close"], errors="coerce")
    bars["code"] = bars["code"].astype(str)
    bars = bars.dropna(subset=["code", "trade_date", "close"])

    this_week = bars[(bars["trade_date"] >= week_start) & (bars["trade_date"] <= week_end)]
    prev_week = bars[(bars["trade_date"] >= prev_week_start) & (bars["trade_date"] <= prev_week_end)]

    if this_week.empty or prev_week.empty:
        LOGGER.warning("本周或上周行情数据不足")
        return []

    # 每股本周均价和上周均价
    this_avg = this_week.groupby("code", sort=False)["close"].mean().rename("this_week_avg")
    prev_avg = prev_week.groupby("code", sort=False)["close"].mean().rename("prev_week_avg")
    week_change = ((this_avg - prev_avg) / prev_avg * 100).rename("week_change_pct")

    # 从 daily_moneyflow 取本周主力净流入
    mf = storage.load_moneyflow(db_path, week_start, week_end, codes=codes)
    if not mf.empty:
        mf["code"] = mf["code"].astype(str)
        mf["net_mf_amount"] = pd.to_numeric(mf["net_mf_amount"], errors="coerce")
        main_net = mf.groupby("code", sort=False)["net_mf_amount"].sum().rename("main_net_in")
    else:
        LOGGER.warning("资金流缓存为空")
        main_net = pd.Series(dtype=float, name="main_net_in")

    # 合并
    result = pd.concat([this_avg, prev_avg, week_change, main_net], axis=1)
    # 筛选：下跌 + 净流入 > 0
    result = result[(result["week_change_pct"] < 0) & (result["main_net_in"] > 0)]
    if result.empty:
        return []

    result = result.sort_values("main_net_in", ascending=False)

    # 补上 name, industry, total_mkt_cap
    base_meta = base[["code", "name", "industry", "total_mkt_cap"]].copy()
    base_meta["code"] = base_meta["code"].astype(str)
    merged = result.reset_index().merge(base_meta, on="code", how="left")
    merged = merged.sort_values("main_net_in", ascending=False)

    rows: list[dict] = []
    total = len(merged)
    for index, row in enumerate(merged.itertuples(index=False), start=1):
        rows.append({
            "code": row.code,
            "name": getattr(row, "name", None),
            "industry": getattr(row, "industry", None),
            "week_change_pct": round(float(row.week_change_pct), 2),
            "main_net_in": round(float(row.main_net_in), 2),
            "total_mkt_cap": getattr(row, "total_mkt_cap", None),
        })
        if progress_callback and index % cfg.progress_save_every == 0:
            progress_callback(index, total, row.code, len(rows))

    if progress_callback:
        progress_callback(total, total, "", len(rows))
    return rows
