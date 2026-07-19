#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""动量因子：过去 N 日收益率。
经典反转/动量因子，用于多因子对比和合成。
数据从 daily_bars 读取，零额外 API。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from . import storage

LOGGER = logging.getLogger("mass_dashboard.momentum")

MOMENTUM_PERIODS = [5, 20, 60]  # 短/中/长期动量


def compute_momentum_panel(db_path: Path, period: int) -> pd.DataFrame:
    """计算动量因子面板：行=trade_date, 列=code, 值=过去period日收益率。

    momentum(t) = close(t)/close(t-period) - 1
    """
    # 需要足够的窗口：取最近 period*2 + 缓冲 天
    with storage._read_conn(db_path) as conn:
        row = conn.execute("SELECT MAX(trade_date) AS d FROM daily_bars").fetchone()
        if not row or not row["d"]:
            return pd.DataFrame()
        end_date = row["d"]
        # 用自然日粗略扩展
        from datetime import datetime, timedelta
        start = (datetime.strptime(end_date, "%Y%m%d") - timedelta(days=period * 3)).strftime("%Y%m%d")
        df = pd.read_sql_query(
            "SELECT trade_date, code, close FROM daily_bars WHERE trade_date BETWEEN ? AND ? AND close IS NOT NULL ORDER BY trade_date, code",
            conn, params=[start, end_date],
        )
    if df.empty:
        return pd.DataFrame()
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["close"])
    panel = df.pivot(index="trade_date", columns="code", values="close").sort_index()
    # 动量 = close(t)/close(t-period) - 1
    momentum = panel / panel.shift(period) - 1
    return momentum


def get_momentum_factor_values(db_path: Path, trade_date: str, period: int = 20) -> pd.Series:
    """取指定交易日的动量因子值（Series: code -> momentum）。"""
    panel = compute_momentum_panel(db_path, period)
    if panel.empty or trade_date not in panel.index:
        return pd.Series(dtype=float)
    return panel.loc[trade_date].dropna()


def compute_volatility_panel(db_path: Path, period: int = 20) -> pd.DataFrame:
    """波动率因子：过去N日日收益率的标准差（年化）。
    高波动=风险大,通常负IC（高波动未来收益差）。
    """
    with storage._read_conn(db_path) as conn:
        row = conn.execute("SELECT MAX(trade_date) AS d FROM daily_bars").fetchone()
        if not row or not row["d"]:
            return pd.DataFrame()
        end_date = row["d"]
        from datetime import datetime, timedelta
        start = (datetime.strptime(end_date, "%Y%m%d") - timedelta(days=period * 4)).strftime("%Y%m%d")
        df = pd.read_sql_query(
            "SELECT trade_date, code, close FROM daily_bars WHERE trade_date BETWEEN ? AND ? AND close IS NOT NULL ORDER BY trade_date, code",
            conn, params=[start, end_date],
        )
    if df.empty:
        return pd.DataFrame()
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["close"])
    panel = df.pivot(index="trade_date", columns="code", values="close").sort_index()
    # 日收益率
    rets = panel.pct_change(fill_method=None)
    # 滚动标准差 * sqrt(252) 年化
    vol = rets.rolling(period).std() * np.sqrt(252)
    return vol


def get_volatility_factor_values(db_path: Path, trade_date: str, period: int = 20) -> pd.Series:
    panel = compute_volatility_panel(db_path, period)
    if panel.empty or trade_date not in panel.index:
        return pd.Series(dtype=float)
    return panel.loc[trade_date].dropna()
