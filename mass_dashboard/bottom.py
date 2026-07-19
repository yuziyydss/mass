#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional, Sequence

import numpy as np
import pandas as pd

import mass_t

from . import storage

LOGGER = logging.getLogger("mass_dashboard.bottom")

# 底部条件参数
VOLUME_WINDOW = 60         # 近60日最大成交量作为基准
RECENT_DAYS = 5            # 近5日成交量需低于基准20%
PRICE_LOOKBACK = 30        # 近30日看不创新低
PRICE_SEGMENTS = 3         # 30日分成3段检查低点递升
RSI_PERIOD = 14            # RSI 计算周期
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9


def calculate_rsi(close: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))


def calculate_macd(close: pd.Series, fast: int = MACD_FAST, slow: int = MACD_SLOW, signal: int = MACD_SIGNAL) -> dict:
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return {"macd_line": macd_line, "signal_line": signal_line}


def check_bottom_for_group(group: pd.DataFrame) -> Optional[dict]:
    """检查单只股票的4个底部条件，输入是已按 trade_date 掆序的该股 daily_bars 子集。"""
    if len(group) < VOLUME_WINDOW + 10:
        return None

    close = pd.to_numeric(group["close"], errors="coerce")
    high = pd.to_numeric(group["high"], errors="coerce")
    low = pd.to_numeric(group["low"], errors="coerce")
    vol = pd.to_numeric(group["vol"], errors="coerce")
    if close.isna().all() or vol.isna().all():
        return None

    # 条件1: 地量 — 近5日成交量均低于近60日最大量的20%
    recent_60 = group.tail(VOLUME_WINDOW)
    vol_high = recent_60["vol"].max()
    recent_5 = group.tail(RECENT_DAYS)
    cond1_volume = bool(
        vol_high > 0
        and all(v < vol_high * 0.2 and v > 0 for v in recent_5["vol"].dropna())
    )

    # 条件2: 不创新低 — 近30日分成3段，每段最低价递升
    recent_30 = group.tail(PRICE_LOOKBACK)
    cond2_price = False
    if len(recent_30) >= PRICE_LOOKBACK:
        lows = []
        for i in range(PRICE_SEGMENTS):
            seg = recent_30.iloc[i * 10:(i + 1) * 10]
            if not seg.empty:
                lows.append(seg["low"].min())
        cond2_price = len(lows) >= PRICE_SEGMENTS and all(lows[i] < lows[i + 1] for i in range(len(lows) - 1))

    # 条件4: 底背离 — 价格创新低但 RSI 或 MACD 不创新低
    rsi = calculate_rsi(close)
    macd = calculate_macd(close)
    cond4_divergence = False
    recent_30_data = group.tail(PRICE_LOOKBACK).copy()
    recent_30_data["rsi"] = rsi.tail(PRICE_LOOKBACK).values
    recent_30_data["macd"] = macd["macd_line"].tail(PRICE_LOOKBACK).values
    if len(recent_30_data) >= PRICE_LOOKBACK:
        price_lows = recent_30_data.nsmallest(3, "low")
        if len(price_lows) >= 2:
            price1 = price_lows.iloc[0]
            price2 = price_lows.iloc[1]
            rsi_div = price1["low"] < price2["low"] and price1["rsi"] > price2["rsi"]
            macd_div = price1["low"] < price2["low"] and price1["macd"] > price2["macd"]
            cond4_divergence = bool(rsi_div or macd_div)

    conditions_met = int(cond1_volume) + int(cond2_price) + int(cond4_divergence)

    return {
        "cond1_volume": int(cond1_volume),
        "cond2_price": int(cond2_price),
        "cond4_divergence": int(cond4_divergence),
        "conditions_met": conditions_met,
        "latest_close": float(close.iloc[-1]) if not close.isna().iloc[-1] else None,
    }


def calculate_bottom_conditions(
    db_path: Path,
    base: pd.DataFrame,
    trade_date: str,
    cfg: mass_t.RuntimeConfig,
    progress_callback: Optional[Callable[[int, int, str, int], None]] = None,
) -> list[dict]:
    """从 daily_bars 缓存 + factor_mass_daily 的 pb/dv_ratio 计算底部4条件。"""
    if base.empty:
        return []

    codes = base["code"].astype(str).tolist()

    # 取近 200 天行情（与 MASS 相同窗口）
    bars = storage.load_daily_bars(
        db_path, start_date=_approx_start(trade_date, 200), end_date=trade_date,
        codes=codes, columns=["code", "trade_date", "open", "high", "low", "close", "vol"],
    )
    if bars.empty:
        LOGGER.warning("本地行情缓存为空，无法计算底部条件")
        return []

    bars["trade_date"] = bars["trade_date"].astype(str)
    bars["code"] = bars["code"].astype(str)
    for col in ["high", "low", "close", "vol"]:
        bars[col] = pd.to_numeric(bars[col], errors="coerce")
    bars = bars.dropna(subset=["code", "trade_date", "close", "vol"])
    bars = bars.sort_values(["code", "trade_date"])

    # 从 factor_mass_daily 取 pb 和 dv_ratio（MASS pipeline 已存）
    pb_map = {}
    dv_ratio_map = {}
    with storage._read_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT code, pb, dv_ratio FROM factor_mass_daily WHERE trade_date=?",
            (trade_date,),
        ).fetchall()
        for r in rows:
            pb_map[r["code"]] = r["pb"]
            dv_ratio_map[r["code"]] = r["dv_ratio"]

    # 条件3: 估值低 — PB<1 或 股息率>3%
    base_codes = set(codes)

    # 按股分组计算
    groups = {code: grp for code, grp in bars.groupby("code", sort=False)}

    rows: list[dict] = []
    total = len(base)
    for index, stock_row in enumerate(base.itertuples(index=False), start=1):
        code = str(stock_row.code)
        grp = groups.get(code)
        if grp is None or len(grp) < VOLUME_WINDOW + 10:
            continue

        result = check_bottom_for_group(grp)
        if result is None:
            continue

        pb_val = pb_map.get(code)
        dv_val = dv_ratio_map.get(code)
        # 条件3: PB<1 或 股息率>3%
        cond3_valuation = bool(
            (pb_val is not None and pb_val < 1) or (dv_val is not None and dv_val > 3)
        )
        result["cond3_valuation"] = int(cond3_valuation)
        result["conditions_met"] += int(cond3_valuation)

        result["code"] = code
        result["name"] = getattr(stock_row, "name", None)
        result["industry"] = getattr(stock_row, "industry", None)
        result["pe_ttm"] = getattr(stock_row, "pe", None)
        result["pb"] = pb_val
        result["dv_ratio"] = dv_val

        rows.append(result)

        if progress_callback and index % cfg.progress_save_every == 0:
            progress_callback(index, total, code, len(rows))

    if progress_callback:
        progress_callback(total, total, "", len(rows))
    return rows


def _approx_start(end_date: str, days: int) -> str:
    """粗略估算起始日期（不需要精确交易日历，load_daily_bars 会自动过滤）。"""
    from datetime import datetime, timedelta
    dt = datetime.strptime(end_date, "%Y%m%d")
    return (dt - timedelta(days=days)).strftime("%Y%m%d")
