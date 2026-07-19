#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pandas as pd

import mass_t

from . import storage

LOGGER = logging.getLogger("mass_dashboard.bottom")

# ── 参数 ──
VOLUME_WINDOW = 60         # 量能基准窗口
VOLUME_SHRINK_RATIO = 0.5  # 近5日均量 < 60日均量的50%（旧版用max×20%太严）
VOLUME_EXPAND_RATIO = 2.0  # 放量确认：量 > 缩量均值 × 2 且收>开
NO_NEW_LOW_DAYS = 10       # 近10日未创60日新低
BREAKOUT_WINDOW = 20       # 突破确认：收 > 近20日最高收
RSI_PERIOD = 14
RSI_OVERSOLD = 35          # 背离只在RSI<35时有效
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
SWING_LOOKSIDE = 5         # 摆动低点：前后各5天更高
SWING_MIN_GAP = 15         # 两个摆动低点至少间隔15天
MIN_HISTORY = 120          # 最低需要120天历史数据

# 申万一级行业PB底部阈值（低于此值视为该行业估值底部）
INDUSTRY_PB_THRESHOLDS = {
    "银行": 0.7, "房地产": 0.8, "公用事业": 1.2, "交通运输": 1.3,
    "建筑装饰": 1.0, "钢铁": 0.9, "采掘": 1.0, "化工": 1.5,
    "有色金属": 1.5, "汽车": 1.5, "家用电器": 1.8, "轻工制造": 1.5,
    "商业贸易": 1.3, "农林牧渔": 1.5, "纺织服装": 1.5,
    "综合": 1.2, "机械设备": 1.5,
}
# 未在阈值表中的行业，用 PB < 行业均值 × 0.6 作为标准
DEFAULT_PB_FACTOR = 0.6
DV_RATIO_MIN = 3.0         # 股息率最低门槛


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


def find_swing_lows(low: pd.Series, lookaside: int = SWING_LOOKSIDE) -> list[int]:
    """找摆动低点：某日low低于前后各lookaside天的low。返回DataFrame行号列表。"""
    swings = []
    for i in range(lookaside, len(low) - lookaside):
        window = low.iloc[i - lookaside: i + lookaside + 1]
        if low.iloc[i] == window.min() and low.iloc[i] < low.iloc[i - 1] and low.iloc[i] < low.iloc[i + 1]:
            swings.append(i)
    return swings


def check_cond1_volume(group: pd.DataFrame) -> bool:
    """条件1: 缩量 + 放量确认。
    近5日均量 < 60日均量的50%（缩量）
    且近10日内至少有1天放量上涨（量 > 缩量均值×2 且 close > open）
    """
    recent_60 = group.tail(VOLUME_WINDOW)
    if len(recent_60) < 20:
        return False
    vol_ma60 = recent_60["vol"].mean()
    if vol_ma60 <= 0:
        return False

    recent_5 = group.tail(5)
    vol_mean_5 = recent_5["vol"].mean()
    shrink = vol_mean_5 < vol_ma60 * VOLUME_SHRINK_RATIO and vol_mean_5 > 0

    if not shrink:
        return False

    # 放量确认：近10日内有1天量>2×缩量均值 且收>开
    recent_10 = group.tail(10)
    expand_threshold = vol_mean_5 * VOLUME_EXPAND_RATIO
    for _, row in recent_10.iterrows():
        vol = pd.to_numeric(row["vol"], errors="coerce")
        close = pd.to_numeric(row["close"], errors="coerce")
        open_ = pd.to_numeric(row["open"], errors="coerce")
        if pd.notna(vol) and pd.notna(close) and pd.notna(open_) and vol > expand_threshold and close > open_:
            return True
    return False


def check_cond2_price(group: pd.DataFrame) -> bool:
    """条件2: 未创60日新低 + 突破20日高点。
    近10日收盘价未创60日新低（止跌）
    且最近收盘 > 近20日最高收盘价（突破确认反转）
    """
    if len(group) < 60:
        return False

    recent_60_close = group.tail(60)["close"]
    min_close_60 = pd.to_numeric(recent_60_close, errors="coerce").min()

    recent_10 = group.tail(NO_NEW_LOW_DAYS)
    recent_10_close = pd.to_numeric(recent_10["close"], errors="coerce")
    # 近10日未创60日新低
    no_new_low = recent_10_close.min() > min_close_60 * 0.999  # 允许0.1%容差

    if not no_new_low:
        return False

    # 突破确认：最近收盘 > 近20日最高收盘
    recent_20_close = pd.to_numeric(group.tail(BREAKOUT_WINDOW)["close"], errors="coerce")
    latest_close = pd.to_numeric(group.iloc[-1]["close"], errors="coerce")
    breakout = latest_close > recent_20_close.max() * 0.999
    return bool(breakout)


def check_cond3_valuation(pb_val: Optional[float], dv_val: Optional[float], pe_val: Optional[float], industry: Optional[str], industry_pb_means: dict) -> bool:
    """条件3: 行业调整PB底部阈值 或 股息率>3%，排除亏损股(PE<0)。
    银行PB<0.7, 地产PB<0.8, 化工PB<1.5 等行业差异化阈值；
    未在阈值表中的行业用 PB < 行业均值 × 0.6；
    股息率>3%也可触发。PE<0(亏损股)不触发。
    """
    # PE<0 的亏损股不算估值低
    if pe_val is not None and pe_val < 0:
        return False

    # 股息率 > 3%
    if dv_val is not None and dv_val > DV_RATIO_MIN:
        return True

    if pb_val is None:
        return False

    # 行业PB阈值
    if industry and industry in INDUSTRY_PB_THRESHOLDS:
        return pb_val < INDUSTRY_PB_THRESHOLDS[industry]

    # 未在阈值表中的行业：PB < 行业均值 × 0.6
    if industry and industry in industry_pb_means:
        return pb_val < industry_pb_means[industry] * DEFAULT_PB_FACTOR

    # 无行业信息时用 PB < 1.5 作为宽标准
    return pb_val < 1.5


def check_cond4_divergence(group: pd.DataFrame) -> bool:
    """条件4: 底背离（改进版）。
    用摆动低点检测，要求两个摆动低点间距≥15天，
    最近摆动低点价格更低但RSI/MACD更高（背离），
    且两个低点处RSI均在超卖区(<35)。
    """
    if len(group) < 60:
        return False

    close = pd.to_numeric(group["close"], errors="coerce")
    low = pd.to_numeric(group["low"], errors="coerce")
    if close.isna().all() or low.isna().all():
        return False

    rsi = calculate_rsi(close)
    macd = calculate_macd(close)

    # 只看近60天的摆动低点（需要足够长窗口）
    recent_60 = group.tail(60).copy()
    recent_60["rsi"] = rsi.tail(60).values
    recent_60["macd"] = macd["macd_line"].tail(60).values
    recent_60_low = pd.to_numeric(recent_60["low"], errors="coerce")

    swings = find_swing_lows(recent_60_low, lookaside=SWING_LOOKSIDE)
    if len(swings) < 2:
        return False

    # 取最后两个摆动低点
    prev_idx = swings[-2]
    curr_idx = swings[-1]

    # 间距≥15天
    if curr_idx - prev_idx < SWING_MIN_GAP:
        return False

    prev_row = recent_60.iloc[prev_idx]
    curr_row = recent_60.iloc[curr_idx]

    # 最近摆动低点价格更低
    if not (curr_row["low"] < prev_row["low"] * 0.999):
        return False

    # RSI在两个低点均<35（超卖区）
    rsi_prev = prev_row["rsi"]
    rsi_curr = curr_row["rsi"]
    rsi_both_oversold = pd.notna(rsi_prev) and pd.notna(rsi_curr) and rsi_prev < RSI_OVERSOLD and rsi_curr < RSI_OVERSOLD

    rsi_div = pd.notna(rsi_prev) and pd.notna(rsi_curr) and rsi_curr > rsi_prev
    macd_div = pd.notna(prev_row["macd"]) and pd.notna(curr_row["macd"]) and curr_row["macd"] > prev_row["macd"]

    # RSI背离只在超卖区才有效；MACD背离无超卖区限制但需MACD<0（零轴下方）
    rsi_valid = rsi_div and rsi_both_oversold
    macd_valid = macd_div and (curr_row["macd"] < 0 or prev_row["macd"] < 0)

    return bool(rsi_valid or macd_valid)


def check_bottom_for_group(
    group: pd.DataFrame,
    pb_val: Optional[float] = None,
    dv_val: Optional[float] = None,
    pe_val: Optional[float] = None,
    industry: Optional[str] = None,
    industry_pb_means: dict = None,
) -> Optional[dict]:
    """检查单只股票的4个底部条件。"""
    if industry_pb_means is None:
        industry_pb_means = {}

    if len(group) < MIN_HISTORY:
        return None

    # 确保数值列可计算
    for col in ["open", "high", "low", "close", "vol"]:
        group[col] = pd.to_numeric(group[col], errors="coerce")
    if group["close"].isna().all() or group["vol"].isna().all():
        return None

    cond1 = check_cond1_volume(group)
    cond2 = check_cond2_price(group)
    cond3 = check_cond3_valuation(pb_val, dv_val, pe_val, industry, industry_pb_means)
    cond4 = check_cond4_divergence(group)

    conditions_met = int(cond1) + int(cond2) + int(cond3) + int(cond4)

    return {
        "cond1_volume": int(cond1),
        "cond2_price": int(cond2),
        "cond3_valuation": int(cond3),
        "cond4_divergence": int(cond4),
        "conditions_met": conditions_met,
        "latest_close": float(group.iloc[-1]["close"]) if pd.notna(group.iloc[-1]["close"]) else None,
    }


def calculate_bottom_conditions(
    db_path: Path,
    base: pd.DataFrame,
    trade_date: str,
    cfg: mass_t.RuntimeConfig,
    progress_callback: Optional[Callable[[int, int, str, int], None]] = None,
) -> list[dict]:
    """从 daily_bars 缓存 + factor_mass_daily 的 pb/dv_ratio/pe 计算底部4条件。"""
    if base.empty:
        return []

    codes = base["code"].astype(str).tolist()

    # 取近 250 天行情（扩大窗口以支持60日新低和swing low检测）
    bars = storage.load_daily_bars(
        db_path, start_date=_approx_start(trade_date, 300), end_date=trade_date,
        codes=codes, columns=["code", "trade_date", "open", "high", "low", "close", "vol"],
    )
    if bars.empty:
        LOGGER.warning("本地行情缓存为空，无法计算底部条件")
        return []

    bars["trade_date"] = bars["trade_date"].astype(str)
    bars["code"] = bars["code"].astype(str)
    for col in ["open", "high", "low", "close", "vol"]:
        bars[col] = pd.to_numeric(bars[col], errors="coerce")
    bars = bars.dropna(subset=["code", "trade_date", "close", "vol"])
    bars = bars.sort_values(["code", "trade_date"])

    # 从 factor_mass_daily 取 pb/dv_ratio/pe
    pb_map = {}
    dv_ratio_map = {}
    pe_map = {}
    with storage._read_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT code, pb, dv_ratio, pe FROM factor_mass_daily WHERE trade_date=?",
            (trade_date,),
        ).fetchall()
        for r in rows:
            pb_map[r["code"]] = r["pb"]
            dv_ratio_map[r["code"]] = r["dv_ratio"]
            pe_map[r["code"]] = r["pe"]

    # 计算各行业PB均值（用于行业调整阈值）
    industry_pb_means = {}
    industry_map = dict(zip(base["code"].astype(str), base["industry"].fillna("")))
    for r in rows:
        ind = industry_map.get(r["code"], "")
        if ind and pd.notna(r["pb"]) and r["pb"] is not None:
            industry_pb_means.setdefault(ind, []).append(float(r["pb"]))
    industry_pb_means = {k: sum(v) / len(v) for k, v in industry_pb_means.items()}

    # 按股分组
    groups = {code: grp for code, grp in bars.groupby("code", sort=False)}

    result_rows: list[dict] = []
    total = len(base)
    for index, stock_row in enumerate(base.itertuples(index=False), start=1):
        code = str(stock_row.code)
        grp = groups.get(code)
        if grp is None or len(grp) < MIN_HISTORY:
            continue

        pb_val = pb_map.get(code)
        dv_val = dv_ratio_map.get(code)
        pe_val = pe_map.get(code)
        industry = industry_map.get(code, "")

        result = check_bottom_for_group(
            grp, pb_val=pb_val, dv_val=dv_val, pe_val=pe_val,
            industry=industry, industry_pb_means=industry_pb_means,
        )
        if result is None:
            continue

        result["code"] = code
        result["name"] = getattr(stock_row, "name", None)
        result["industry"] = industry
        result["pe_ttm"] = pe_val
        result["pb"] = pb_val
        result["dv_ratio"] = dv_val

        result_rows.append(result)

        if progress_callback and index % cfg.progress_save_every == 0:
            progress_callback(index, total, code, len(result_rows))

    if progress_callback:
        progress_callback(total, total, "", len(result_rows))
    return result_rows


def _approx_start(end_date: str, days: int) -> str:
    from datetime import datetime, timedelta
    dt = datetime.strptime(end_date, "%Y%m%d")
    return (dt - timedelta(days=days)).strftime("%Y%m%d")
