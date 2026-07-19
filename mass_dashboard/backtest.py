#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""回测引擎：按因子选 top-N 等权持仓，滚动调仓，计算累计净值/夏普/最大回撤。

对齐聚宽回测引擎的核心能力。数据全部从本地 SQLite：
- 因子值：factor_mass_daily
- 收盘价：daily_bars

方法学：
- 每个截面日 t，按因子值排序选前 N 只，等权买入
- 持仓 hold_days 个交易日后换仓（非每日换仓，减少交易成本噪声）
- 收益 = 持仓股 t 到 t+hold_days 的收益的等权平均
- 累计净值 = ∏(1+r)
- 夏普 = mean(r)/std(r)*sqrt(252/hold_days) 年化
- 最大回撤 = max(峰值-谷值)/峰值
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from . import storage

LOGGER = logging.getLogger("mass_dashboard.backtest")


def run_backtest(
    db_path: Path,
    factor_col: str = "mass_zscore",
    top_n: int = 50,
    hold_days: int = 5,
    direction: str = "top",  # top=选高因子, bottom=选低因子
    benchmark: str = "equal",  # equal=等权全市场基准
    cost_bps: float = 10.0,  # 单边交易成本(基点),换仓时扣除
    weight: str = "equal",  # equal=等权, factor=按因子值加权
) -> dict:
    """按因子选股回测。

    Args:
        factor_col: 因子列名（mass_zscore/mass_neu/mass_raw）
        top_n: 选股数量
        hold_days: 持仓周期（交易日），到期换仓
        direction: top=选因子最高的N只, bottom=选最低的
        benchmark: 基准策略，equal=等权全市场

    Returns:
        dict: 累计净值、收益序列、夏普、最大回撤、每期持仓数等
    """
    factor_panel = storage.load_factor_panel(db_path, factor_col=factor_col)
    if factor_panel.empty:
        return {"error": "因子面板为空"}

    # 收盘价
    close_panel = storage.load_close_panel(db_path, factor_panel.index[0], factor_panel.index[-1])
    if close_panel.empty:
        return {"error": "收盘价面板为空"}

    # 只保留两个面板公共的交易日（历史CSV导入的因子日期可能早于行情缓存，需过滤）
    common_dates = factor_panel.index.intersection(close_panel.index).tolist()
    factor_panel = factor_panel.loc[common_dates]
    close_panel = close_panel.loc[common_dates]
    dates = common_dates
    if len(dates) < 3:
        return {"error": f"公共交易日不足（仅 {len(dates)} 个），无法回测"}

    # 在每个截面日，选股 + 算到下一换仓日的收益
    # 换仓点：dates[0], dates[hold_days], dates[2*hold_days], ...
    rebalance_indices = list(range(0, len(dates) - 1, hold_days))
    if not rebalance_indices:
        return {"error": "无法换仓（数据太少）"}

    portfolio_returns = []  # 每个持仓期的组合收益
    benchmark_returns = []  # 基准收益
    holdings_count = []
    rebalance_dates = []
    last_holdings = []  # 最后一期持仓明细
    hhi_values = []  # 持仓集中度

    for i in rebalance_indices:
        date = dates[i]
        # 需要有 t+hold_days 的价格来算收益
        if i + hold_days >= len(dates):
            # 用最后一个可得日
            next_idx = len(dates) - 1
            if next_idx == i:
                continue
        else:
            next_idx = i + hold_days
        next_date = dates[next_idx]

        factor_row = factor_panel.loc[date].dropna()
        if len(factor_row) < top_n * 2:
            continue

        # 选股
        if direction == "bottom":
            selected = factor_row.nsmallest(top_n).index.tolist()
        else:
            selected = factor_row.nlargest(top_n).index.tolist()

        # 收益：每只 t->next_date 的收益，等权平均
        if date not in close_panel.index or next_date not in close_panel.index:
            continue
        p0 = close_panel.loc[date, selected].astype(float)
        p1 = close_panel.loc[next_date, selected].astype(float)
        valid = ~(p0.isna() | p1.isna()) & (p0 > 0)
        if valid.sum() == 0:
            continue
        stock_rets = (p1[valid] / p0[valid] - 1)
        # 权重：equal=等权, factor=按因子值加权(softmax归一化)
        if weight == "factor":
            f_vals = factor_panel.loc[date, valid.index].astype(float)
            exp_vals = np.exp(f_vals - f_vals.max())
            w_series = exp_vals / exp_vals.sum()
            w = w_series
            port_ret = float((stock_rets * w).sum())
        else:
            w = pd.Series(1.0/len(valid), index=valid.index)
            port_ret = float(stock_rets.mean())
        # 持仓集中度 HHI (归一化到0-1, 1=单只满仓, 1/n=完全等权)
        w_sq = float((w ** 2).sum())
        hhi = w_sq
        # 扣交易成本：每次换仓买卖双边，cost_bps基点
        port_ret -= 2 * cost_bps / 10000.0
        portfolio_returns.append(port_ret)
        holdings_count.append(int(valid.sum()))
        rebalance_dates.append(date)
        hhi_values.append(hhi)
        last_holdings = [{"code": c, "weight": round(1.0/valid.sum(), 4), "return": round(float(stock_rets[c]), 4)}
                         for c in valid.index[valid.values].tolist()]
        if benchmark == "equal":
            all_p0 = close_panel.loc[date].dropna()
            all_p1 = close_panel.loc[next_date].dropna()
            common = all_p0.index.intersection(all_p1.index)
            common = [c for c in common if all_p0[c] > 0]
            if common:
                b_rets = (all_p1[common] / all_p0[common] - 1)
                benchmark_returns.append(float(b_rets.mean()))
            else:
                benchmark_returns.append(0.0)

    if not portfolio_returns:
        return {"error": "回测无有效持仓期"}

    port_returns = np.array(portfolio_returns)
    bench_returns = np.array(benchmark_returns) if len(benchmark_returns) == len(portfolio_returns) else None

    # 累计净值
    port_cum = np.cumprod(1 + port_returns)
    excess_returns = port_returns - bench_returns if bench_returns is not None else port_returns
    excess_cum = np.cumprod(1 + excess_returns)

    # 夏普：年化
    if len(port_returns) > 1 and port_returns.std(ddof=1) > 0:
        # 每个持仓期 = hold_days 个交易日，年化因子 = 252/hold_days
        sharpe = float(port_returns.mean() / port_returns.std(ddof=1) * np.sqrt(252 / hold_days))
    else:
        sharpe = None

    # 最大回撤
    peak = np.maximum.accumulate(port_cum)
    drawdowns = (peak - port_cum) / peak
    max_drawdown = float(drawdowns.max()) if len(drawdowns) > 0 else 0.0

    # 年化收益 + 年化波动率 + Calmar
    periods_per_year = 252 / hold_days
    total_return = float(port_cum[-1] - 1)
    n_years = len(port_returns) / periods_per_year
    annualized_return = float((1 + total_return) ** (1 / n_years) - 1) if n_years > 0 else None
    annualized_volatility = float(port_returns.std(ddof=1) * np.sqrt(periods_per_year)) if len(port_returns) > 1 else None
    calmar = float(annualized_return / max_drawdown) if (annualized_return is not None and max_drawdown > 0) else None

    # 基准统计
    bench_cum = np.cumprod(1 + bench_returns) if bench_returns is not None else None

    return {
        "factor": factor_col,
        "top_n": top_n,
        "hold_days": hold_days,
        "direction": direction,
        "n_periods": len(portfolio_returns),
        "rebalance_dates": rebalance_dates,
        "portfolio_cum": [round(float(x), 6) for x in port_cum],
        "benchmark_cum": [round(float(x), 6) for x in bench_cum] if bench_cum is not None else [],
        "excess_cum": [round(float(x), 6) for x in excess_cum],
        "portfolio_returns": [round(float(x), 6) for x in port_returns],
        "total_return": round(float(port_cum[-1] - 1), 4),
        "benchmark_return": round(float(bench_cum[-1] - 1), 6) if bench_cum is not None else None,
        "excess_return": round(float(excess_cum[-1] - 1), 4),
        "sharpe": round(sharpe, 4) if sharpe is not None else None,
        "max_drawdown": round(max_drawdown, 4),
        "annualized_return": round(annualized_return, 4) if annualized_return is not None else None,
        "annualized_volatility": round(annualized_volatility, 4) if annualized_volatility is not None else None,
        "calmar": round(calmar, 4) if calmar is not None else None,
        "win_rate": round(float((port_returns > 0).mean()), 4),
        "avg_holdings": round(float(np.mean(holdings_count)), 1),
        "avg_hhi": round(float(np.mean(hhi_values)), 4),  # 持仓集中度均值
        "last_holdings": last_holdings,
        "date_range": [rebalance_dates[0], rebalance_dates[-1]],
    }
