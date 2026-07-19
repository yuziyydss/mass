#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""因子有效性分析：IC/IR + 分层回测。

对齐聚宽因子分析的核心能力。所有数据从本地 SQLite 读取，零额外 API：
- 因子值面板：factor_mass_daily
- 收盘价面板：daily_bars

方法学（量化界标准做法）：
- IC（信息系数）= 每个截面日，因子值 vs 未来N日收益的 Spearman 秩相关
  用秩相关而非Pearson，更稳健、对极值不敏感
- IR（信息比率）= mean(IC) / std(IC) × sqrt(252) 年化
- 分层回测：每个截面日按因子值排序分N组，算每组等权未来N日收益
  多空收益 = 第N组(因子最高)收益 - 第1组(因子最低)收益，累计成净值曲线
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from . import storage

LOGGER = logging.getLogger("mass_dashboard.factor_analysis")

DEFAULT_FORWARD_DAYS = [1, 5, 10, 20]  # 多个前瞻周期
DEFAULT_N_QUANTILES = 5


def compute_forward_returns(close_panel: pd.DataFrame, periods: list[int]) -> dict[int, pd.DataFrame]:
    """计算未来N日收益面板。返回 {N: DataFrame}，结构与 close_panel 相同。

    fwd_ret(t, N) = close(t+N) / close(t) - 1
    """
    fwd_returns = {}
    for N in periods:
        # pct_change 会算 close(t)/close(t-N) - 1，但我们要的是未来收益 close(t+N)/close(t) - 1
        # 即 shift(-N) / current - 1
        future = close_panel.shift(-N)
        fwd = future / close_panel - 1
        fwd_returns[N] = fwd
    return fwd_returns


def compute_ic_series(
    factor_panel: pd.DataFrame,
    fwd_returns: dict[int, pd.DataFrame],
) -> dict[int, pd.DataFrame]:
    """计算每个截面日、每个前瞻周期的 IC（Spearman秩相关）。

    返回 {N: DataFrame(index=trade_date, columns=['ic', 'n_stocks'])}
    """
    ic_results = {}
    for N, fwd_panel in fwd_returns.items():
        ics = []
        n_stocks = []
        dates = []
        for date in factor_panel.index:
            if date not in fwd_panel.index:
                continue
            factor_row = factor_panel.loc[date].dropna()
            fwd_row = fwd_panel.loc[date].dropna()
            # 取交集：既有因子值又有未来收益的股票
            common = factor_row.index.intersection(fwd_row.index)
            if len(common) < 20:  # 样本太少不算
                continue
            f_vals = factor_row.loc[common].astype(float)
            r_vals = fwd_row.loc[common].astype(float)
            # 去掉零方差（全部相同）的情况
            if f_vals.nunique() < 2 or r_vals.nunique() < 2:
                continue
            try:
                ic, _ = spearmanr(f_vals, r_vals)
            except Exception:
                continue
            if pd.isna(ic):
                continue
            ics.append(float(ic))
            n_stocks.append(len(common))
            dates.append(date)
        ic_results[N] = pd.DataFrame(
            {"ic": ics, "n_stocks": n_stocks}, index=dates
        )
    return ic_results


def summarize_ic(ic_results: dict[int, pd.DataFrame]) -> list[dict]:
    """汇总每个前瞻周期的 IC 统计：均值、IR、胜率等。"""
    summary = []
    for N, ic_df in ic_results.items():
        if ic_df.empty:
            summary.append({"forward_days": N, "ic_mean": None, "ir": None, "ic_std": None,
                            "win_rate": None, "n_periods": 0})
            continue
        ics = ic_df["ic"]
        ic_mean = float(ics.mean())
        ic_std = float(ics.std(ddof=1))
        ir = float(ic_mean / ic_std * np.sqrt(252)) if ic_std > 0 else None
        win_rate = float((ics > 0).mean())
        summary.append({
            "forward_days": N,
            "ic_mean": round(ic_mean, 4),
            "ic_std": round(ic_std, 4),
            "ir": round(ir, 4) if ir is not None else None,
            "win_rate": round(win_rate, 4),
            "n_periods": int(len(ics)),
        })
    return summary


def compute_quantile_returns(
    factor_panel: pd.DataFrame,
    close_panel: pd.DataFrame,
    forward_days: int,
    n_quantiles: int = DEFAULT_N_QUANTILES,
) -> dict:
    """分层回测：每个截面日按因子值分n组，算每组等权未来N日收益。

    返回:
    - quantile_daily: 每日每组的前瞻收益
    - cum_returns: 每组累计净值（按截面日滚动复利）
    - long_short_cum: 多空累计净值
    """
    if factor_panel.empty or close_panel.empty:
        return {}

    # 未来N日收益
    fwd = close_panel.shift(-forward_days) / close_panel - 1

    # 对齐两个面板的日期和股票
    common_dates = factor_panel.index.intersection(fwd.index)
    if len(common_dates) < 2:
        return {}

    quantile_daily_rows = []
    for date in common_dates:
        factor_row = factor_panel.loc[date].dropna()
        fwd_row = fwd.loc[date].dropna()
        common = factor_row.index.intersection(fwd_row.index)
        if len(common) < n_quantiles * 2:
            continue
        f_vals = factor_row.loc[common].astype(float)
        r_vals = fwd_row.loc[common].astype(float)
        # 按因子值排序，分n组
        df = pd.DataFrame({"factor": f_vals, "ret": r_vals}).sort_values("factor")
        # qcut 分组；用 labels=False 得到 0..n-1
        try:
            df["q"] = pd.qcut(df["factor"], n_quantiles, labels=False, duplicates="drop")
        except Exception:
            continue
        if df["q"].nunique() < 2:
            continue
        grp_ret = df.groupby("q")["ret"].mean()
        record = {"trade_date": date}
        for q in range(n_quantiles):
            record[f"q{q}"] = round(float(grp_ret.get(q, 0)), 6)
        record["long_short"] = round(float(grp_ret.get(n_quantiles - 1, 0) - grp_ret.get(0, 0)), 6)
        quantile_daily_rows.append(record)

    if not quantile_daily_rows:
        return {}

    qd = pd.DataFrame(quantile_daily_rows).set_index("trade_date").sort_index()

    # 累计净值：把每日前瞻收益"贴现"到截面日上滚动复利
    # 简化处理：逐截面日 (1+r) 连乘
    cum = (1 + qd).cumprod()
    result = {
        "forward_days": forward_days,
        "n_quantiles": n_quantiles,
        "n_periods": len(qd),
        "quantile_avg": {
            f"q{q}": round(float(qd[f"q{q}"].mean()), 6) for q in range(n_quantiles)
        },
        "long_short_avg": round(float(qd["long_short"].mean()), 6),
        "dates": qd.index.tolist(),
        "quantile_cum": {f"q{q}": [round(float(x), 6) for x in cum[f"q{q}"].tolist()] for q in range(n_quantiles)},
        "long_short_cum": [round(float(x), 6) for x in cum["long_short"].tolist()],
    }
    return result


def analyze_factor_from_panels(
    factor_panel: pd.DataFrame,
    close_panel: pd.DataFrame,
    forward_days_list: list[int],
    n_quantiles: int = DEFAULT_N_QUANTILES,
) -> dict:
    """直接接受面板数据的分析入口（用于测试/复用，不读DB）。"""
    if factor_panel.empty or close_panel.empty:
        return {"error": "面板为空"}

    fwd_returns = compute_forward_returns(close_panel, forward_days_list)
    ic_results = compute_ic_series(factor_panel, fwd_returns)
    ic_summary = summarize_ic(ic_results)

    main_N = 5 if 5 in forward_days_list else forward_days_list[0]
    main_ic = ic_results.get(main_N, pd.DataFrame())
    ic_dates = main_ic.index.tolist()
    ic_values = [round(float(x), 4) for x in main_ic["ic"].tolist()] if not main_ic.empty else []
    cum_ic = [round(float(x), 4) for x in main_ic["ic"].cumsum().tolist()] if not main_ic.empty else []

    quantile_result = compute_quantile_returns(factor_panel, close_panel, main_N, n_quantiles)

    return {
        "factor": "synthetic",
        "forward_days_list": forward_days_list,
        "n_dates": len(factor_panel.index),
        "date_range": [str(factor_panel.index[0]), str(factor_panel.index[-1])],
        "ic_summary": ic_summary,
        "ic_dates": ic_dates,
        "ic_values": ic_values,
        "cum_ic": cum_ic,
        "main_forward_days": main_N,
        "quantile": quantile_result,
    }


def analyze_factor(
    db_path: Path,
    factor_col: str = "mass_zscore",
    forward_days_list: Optional[list[int]] = None,
    n_quantiles: int = DEFAULT_N_QUANTILES,
) -> dict:
    """完整因子分析：IC/IR + 分层回测。"""
    if forward_days_list is None:
        forward_days_list = DEFAULT_FORWARD_DAYS

    factor_panel = storage.load_factor_panel(db_path, factor_col=factor_col)
    if factor_panel.empty:
        LOGGER.warning("因子面板为空")
        return {"error": "因子面板为空"}

    dates = factor_panel.index.tolist()
    # 收盘价窗口：需要覆盖最早因子日 到 最晚因子日+最长前瞻周期
    # 用自然日粗略扩展两端
    start = dates[0]
    end = dates[-1]
    close_panel = storage.load_close_panel(db_path, start, end)
    if close_panel.empty:
        LOGGER.warning("收盘价面板为空")
        return {"error": "收盘价面板为空"}

    fwd_returns = compute_forward_returns(close_panel, forward_days_list)
    ic_results = compute_ic_series(factor_panel, fwd_returns)
    ic_summary = summarize_ic(ic_results)

    # IC 时序（用5日前瞻做主图）
    main_N = 5 if 5 in forward_days_list else forward_days_list[0]
    main_ic = ic_results.get(main_N, pd.DataFrame())
    ic_dates = main_ic.index.tolist()
    ic_values = [round(float(x), 4) for x in main_ic["ic"].tolist()] if not main_ic.empty else []
    cum_ic = [round(float(x), 4) for x in main_ic["ic"].cumsum().tolist()] if not main_ic.empty else []

    # 分层回测（用5日前瞻）
    quantile_result = compute_quantile_returns(factor_panel, close_panel, main_N, n_quantiles)

    return {
        "factor": factor_col,
        "forward_days_list": forward_days_list,
        "n_dates": len(dates),
        "date_range": [dates[0], dates[-1]],
        "ic_summary": ic_summary,
        "ic_dates": ic_dates,
        "ic_values": ic_values,
        "cum_ic": cum_ic,
        "main_forward_days": main_N,
        "quantile": quantile_result,
    }


def compare_factors(db_path, factor_specs: list[dict]) -> list[dict]:
    """横向对比多个因子的 IC/IR。factor_specs: [{name, panel}] 或内置因子名。

    返回每个因子的简明 IC 汇总（5日/10日 IC均值 + IR）。
    """
    from . import momentum
    results = []
    # 先加载 close_panel 一次复用
    with storage._read_conn(db_path) as conn:
        row = conn.execute("SELECT MIN(trade_date) AS mn, MAX(trate_date) AS mx FROM daily_bars".replace("trate_date","trade_date")).fetchone()
    if not row or not row["mn"]:
        return []
    close_panel = storage.load_close_panel(db_path, row["mn"], row["mx"])
    if close_panel.empty:
        return []

    for spec in factor_specs:
        name = spec.get("name", "?")
        panel = spec.get("panel")
        if panel is None:
            # 内置因子
            if name.startswith("momentum"):
                parts = name.split("_")
                period = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 20
                panel = momentum.compute_momentum_panel(db_path, period)
            elif name in ("mass_zscore", "mass_neu", "mass_raw"):
                panel = storage.load_factor_panel(db_path, name)
            else:
                continue
        if panel is None or panel.empty:
            continue
        common = panel.index.intersection(close_panel.index).tolist()
        if len(common) < 3:
            continue
        fwd = compute_forward_returns(close_panel.loc[common], [5, 10])
        ic = compute_ic_series(panel.loc[common], fwd)
        summary = summarize_ic(ic)
        results.append({
            "factor": name,
            "ic_5": summary[0].get("ic_mean") if len(summary) > 0 else None,
            "ir_5": summary[0].get("ir") if len(summary) > 0 else None,
            "ic_10": summary[1].get("ic_mean") if len(summary) > 1 else None,
            "ir_10": summary[1].get("ir") if len(summary) > 1 else None,
            "n_dates": len(common),
        })
    return results
