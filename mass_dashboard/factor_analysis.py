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


def _load_factor_panel_by_name(db_path, name: str):
    """按因子名加载面板，统一处理 MASS/动量/波动率/换手率。返回 None 表示未知因子。"""
    from . import momentum
    parts = name.split("_")
    period = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 20
    if name.startswith("momentum"):
        return momentum.compute_momentum_panel(db_path, period)
    if name.startswith("volatility"):
        return momentum.compute_volatility_panel(db_path, period)
    if name.startswith("turnover"):
        return momentum.compute_turnover_panel(db_path, period)
    if name.startswith("moneyflow"):
        return momentum.compute_moneyflow_factor_panel(db_path, period)
    if name in ("mass_zscore", "mass_neu", "mass_raw"):
        return storage.load_factor_panel(db_path, name)
    return None


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

    # 并行加载各因子面板（IO密集, SQLite WAL + 线程本地读连接支持并发读）
    from concurrent.futures import ThreadPoolExecutor
    names = [s.get("name", "?") for s in factor_specs]
    with ThreadPoolExecutor(max_workers=4) as ex:
        panels = list(ex.map(lambda n: _load_factor_panel_by_name(db_path, n), names))

    for name, panel in zip(names, panels):
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


def synthesize_factor(db_path, components: list, weights: list[float] = None) -> "pd.DataFrame":
    """多因子合成：加权合成多个因子面板，先截面标准化再加权。

    components: 因子名列表，如 ["momentum_5", "volatility_20"]
    weights: 权重，默认等权；动量正贡献，波动率负贡献自动处理
    返回合成面板（行=日期,列=code）。
    """
    from . import momentum
    if weights is None:
        weights = [1.0] * len(components)
    panels = []
    for name in components:
        if name.startswith("momentum"):
            p = momentum.compute_momentum_panel(db_path, int(name.split("_")[1]))
        elif name.startswith("volatility"):
            p = momentum.compute_volatility_panel(db_path, int(name.split("_")[1]))
        elif name in ("mass_zscore", "mass_neu", "mass_raw"):
            p = storage.load_factor_panel(db_path, name)
        else:
            continue
        if not p.empty:
            panels.append((name, p))
    if not panels:
        return pd.DataFrame()
    # 取公共日期
    common_idx = panels[0][1].index
    for _, p in panels[1:]:
        common_idx = common_idx.intersection(p.index)
    if len(common_idx) < 3:
        return pd.DataFrame()
    # 截面标准化 + 加权合成
    synthesized = pd.DataFrame(index=common_idx)
    for (name, p), w in zip(panels, weights):
        sub = p.loc[common_idx]
        # 截面zscore
        mu = sub.mean(axis=1)
        sd = sub.std(axis=1)
        z = sub.sub(mu, axis=0).div(sd.replace(0, np.nan), axis=0)
        synthesized[name] = z.mean(axis=1)  # placeholder
    # 加权
    result = pd.DataFrame(index=common_idx, columns=panels[0][1].columns, dtype=float)
    for (name, p), w in zip(panels, weights):
        sub = p.loc[common_idx]
        mu = sub.mean(axis=1)
        sd = sub.std(axis=1)
        z = sub.sub(mu, axis=0).div(sd.replace(0, np.nan), axis=0)
        result = result.add(w * z, fill_value=0)
    return result


def factor_distribution(db_path, factor_col: str = "mass_zscore", n_bins: int = 30) -> dict:
    """最新截面因子值的分布直方图 + 统计。"""
    panel = storage.load_factor_panel(db_path, factor_col=factor_col)
    if panel.empty:
        return {"error": "因子面板为空"}
    latest = panel.index[-1]
    values = panel.loc[latest].dropna()
    if len(values) < 10:
        return {"error": "样本不足"}
    # 统计
    import numpy as np
    mu = float(values.mean())
    sigma = float(values.std(ddof=1))
    # 直方图
    lo, hi = values.min(), values.max()
    bins = np.linspace(lo, hi, n_bins + 1)
    counts, _ = np.histogram(values, bins=bins)
    return {
        "factor": factor_col,
        "trade_date": latest,
        "n": int(len(values)),
        "mean": round(mu, 4),
        "std": round(sigma, 4),
        "min": round(float(lo), 4),
        "max": round(float(hi), 4),
        "skew": round(float(values.skew()), 4),
        "kurt": round(float(values.kurt()), 4),
        "bins": [round(float(b), 4) for b in bins],
        "counts": [int(c) for c in counts],
    }


def neutralized_ic(db_path, factor_col: str = "mass_zscore", forward_days: int = 5) -> dict:
    """行业+市值中性化后的IC vs 原始IC对比。
    用残差(因子对行业dummy+log市值回归)再算IC,看因子去掉行业/市值暴露后还剩多少预测力。
    """
    import numpy as np
    panel = storage.load_factor_panel(db_path, factor_col=factor_col)
    if panel.empty:
        return {"error": "因子面板为空"}
    close_panel = storage.load_close_panel(db_path, panel.index[0], panel.index[-1])
    common = panel.index.intersection(close_panel.index).tolist()
    if len(common) < 3:
        return {"error": "公共日期不足"}

    # 取行业和市值（从factor_mass_daily）
    with storage._read_conn(db_path) as conn:
        meta = pd.read_sql_query(
            "SELECT trade_date, code, industry, total_mkt_cap FROM factor_mass_daily WHERE mass_zscore IS NOT NULL",
            conn,
        )
    meta["total_mkt_cap"] = pd.to_numeric(meta["total_mkt_cap"], errors="coerce")

    # 前瞻收益
    fwd = close_panel.shift(-forward_days) / close_panel - 1

    orig_ics = []
    neut_ics = []
    for date in common:
        if date not in fwd.index:
            continue
        f_row = panel.loc[date].dropna()
        r_row = fwd.loc[date].dropna()
        common_codes = f_row.index.intersection(r_row.index)
        if len(common_codes) < 30:
            continue
        # 原始IC
        from scipy.stats import spearmanr
        try:
            ic_orig, _ = spearmanr(f_row.loc[common_codes].astype(float), r_row.loc[common_codes].astype(float))
        except Exception:
            continue
        if pd.isna(ic_orig):
            continue
        # 中性化残差
        meta_row = meta[(meta["trade_date"] == date) & (meta["code"].isin(common_codes))].set_index("code")
        meta_codes = meta_row.index.intersection(common_codes)
        if len(meta_codes) < 30:
            continue
        f_sub = f_row.loc[meta_codes].astype(float)
        r_sub = r_row.loc[meta_codes].astype(float)
        caps = meta_row.loc[meta_codes, "total_mkt_cap"].astype(float)
        inds = meta_row.loc[meta_codes, "industry"].fillna("未分类")
        # X = industry dummies + log(cap)
        ind_dum = pd.get_dummies(inds, dummy_na=True)
        log_cap = np.log(caps.clip(lower=1))
        X = pd.concat([ind_dum, log_cap], axis=1)
        X.insert(0, "const", 1.0)
        X_mat = X.values.astype(float)
        # 对因子回归取残差
        try:
            coef_f, _, _, _ = np.linalg.lstsq(X_mat, f_sub.values, rcond=None)
            resid_f = f_sub.values - X_mat @ coef_f
            coef_r, _, _, _ = np.linalg.lstsq(X_mat, r_sub.values, rcond=None)
            resid_r = r_sub.values - X_mat @ coef_r
            ic_neut, _ = spearmanr(resid_f, resid_r)
        except Exception:
            continue
        if not pd.isna(ic_neut):
            orig_ics.append(float(ic_orig))
            neut_ics.append(float(ic_neut))

    if not orig_ics:
        return {"error": "有效IC期数不足"}
    orig_mean = float(np.mean(orig_ics))
    neut_mean = float(np.mean(neut_ics))
    return {
        "factor": factor_col,
        "forward_days": forward_days,
        "n_periods": len(orig_ics),
        "orig_ic": round(orig_mean, 4),
        "neutralized_ic": round(neut_mean, 4),
        "ic_retained": round(neut_mean / orig_mean, 4) if abs(orig_mean) > 1e-9 else None,
        "orig_ics": [round(x, 4) for x in orig_ics],
        "neut_ics": [round(x, 4) for x in neut_ics],
    }


def factor_returns(db_path, factors: list[str], forward_days: int = 5) -> list[dict]:
    """各因子的分层多空收益对比(单因子贡献归因)。
    对每个因子,算分5层多空平均收益,看哪个因子贡献最大。
    """
    from . import momentum
    results = []
    close_panel = None
    for name in factors:
        if name.startswith("momentum"):
            panel = momentum.compute_momentum_panel(db_path, int(name.split("_")[1]))
        elif name.startswith("volatility"):
            panel = momentum.compute_volatility_panel(db_path, int(name.split("_")[1]))
        elif name.startswith("turnover"):
            panel = momentum.compute_turnover_panel(db_path, int(name.split("_")[1]))
        elif name in ("mass_zscore","mass_neu","mass_raw"):
            panel = storage.load_factor_panel(db_path, name)
        else:
            continue
        if panel.empty:
            continue
        if close_panel is None:
            close_panel = storage.load_close_panel(db_path, panel.index[0], panel.index[-1])
        common = panel.index.intersection(close_panel.index).tolist()
        if len(common) < 3:
            continue
        q = compute_quantile_returns(panel.loc[common], close_panel.loc[common], forward_days, 5)
        if not q:
            continue
        results.append({
            "factor": name,
            "long_short_avg": q.get("long_short_avg"),
            "n_periods": q.get("n_periods"),
        })
    results.sort(key=lambda x: abs(x.get("long_short_avg") or 0), reverse=True)
    return results


def build_portfolio(db_path, components: list[dict], top_n: int = 30) -> dict:
    """选股策略引擎:多因子加权综合评分,选top-N组合。
    components: [{name, weight, sign}] sign=1表示正向因子(高=好),-1表示反向因子(低=好,如波动率/换手率)
    返回 {date, scores: [{code, score, factors}], top: [...]}
    """
    from . import momentum
    import numpy as np
    # 加载各因子最新截面
    panels = {}
    for comp in components:
        name = comp["name"]
        if name.startswith("momentum"):
            p = momentum.compute_momentum_panel(db_path, int(name.split("_")[1]))
        elif name.startswith("volatility"):
            p = momentum.compute_volatility_panel(db_path, int(name.split("_")[1]))
        elif name.startswith("turnover"):
            p = momentum.compute_turnover_panel(db_path, int(name.split("_")[1]))
        elif name in ("mass_zscore","mass_neu","mass_raw"):
            p = storage.load_factor_panel(db_path, name)
        else:
            continue
        if not p.empty:
            panels[name] = p

    if not panels:
        return {"error": "无可用因子"}
    # 取所有因子公共的最新截面日
    common_dates = None
    for p in panels.values():
        idx = set(p.index)
        common_dates = idx if common_dates is None else common_dates & idx
    if not common_dates:
        return {"error": "因子无公共日期"}
    latest = max(common_dates)

    # 构建综合得分 DataFrame
    score = None
    factor_values = {}
    for comp in components:
        name = comp["name"]
        if name not in panels:
            continue
        weight = comp.get("weight", 1.0)
        sign = comp.get("sign", 1)
        row = panels[name].loc[latest].dropna()
        # 截面zscore标准化
        mu, sd = row.mean(), row.std()
        if sd == 0 or pd.isna(sd):
            continue
        z = (row - mu) / sd
        z = z * sign * weight
        if score is None:
            score = z.copy()
        else:
            score = score.add(z, fill_value=0)
        factor_values[name] = row

    if score is None or score.empty:
        return {"error": "无法计算综合得分"}

    # 取top_n
    top = score.nlargest(top_n)
    with storage._read_conn(db_path) as conn:
        if not top.empty:
            placeholders = ",".join(["?"]*len(top.index))
            meta = pd.read_sql_query(
                f"SELECT code, name, industry FROM factor_mass_daily WHERE trade_date=? AND code IN ({placeholders})",
                conn, params=[latest]+list(top.index),
            ).set_index("code") if len(top) > 0 else pd.DataFrame()
        else:
            meta = pd.DataFrame()

    top_list = []
    for code, sc in top.items():
        m = meta.loc[code] if code in meta.index else {}
        top_list.append({
            "code": code,
            "score": round(float(sc), 4),
            "name": m.get("name","") if isinstance(m, pd.Series) else "",
            "industry": m.get("industry","") if isinstance(m, pd.Series) else "",
            "factors": {n: round(float(factor_values[n].get(code, float("nan"))), 4) for n in panels if code in factor_values[n].index},
        })
    return {
        "date": latest,
        "n_candidates": len(score),
        "top": top_list,
    }


def ic_heatmap(db_path, factor_col: str = "mass_zscore", forward_days_list: list = None) -> dict:
    """IC热力图:日期×前瞻周期。返回 {dates, periods, matrix}"""
    if forward_days_list is None:
        forward_days_list = [1, 5, 10, 20]
    panel = storage.load_factor_panel(db_path, factor_col=factor_col)
    if panel.empty:
        return {"error": "因子面板为空"}
    close_panel = storage.load_close_panel(db_path, panel.index[0], panel.index[-1])
    common = panel.index.intersection(close_panel.index).tolist()
    if len(common) < 3:
        return {"error": "公共日期不足"}
    fwd = compute_forward_returns(close_panel.loc[common], forward_days_list)
    ic = compute_ic_series(panel.loc[common], fwd)
    # matrix: dates × periods
    all_dates = set()
    for N in forward_days_list:
        if N in ic and not ic[N].empty:
            all_dates.update(ic[N].index.tolist())
    dates = sorted(all_dates)
    matrix = []
    for d in dates:
        row = []
        for N in forward_days_list:
            df = ic.get(N, pd.DataFrame())
            if d in df.index:
                row.append(round(float(df.loc[d, "ic"]), 4))
            else:
                row.append(None)
        matrix.append(row)
    return {"dates": dates, "periods": forward_days_list, "matrix": matrix}


def factor_decay_report(db_path, factors: list[str] = None, max_period: int = 20) -> list[dict]:
    """各因子IC随前瞻周期衰减报告。
    对每个因子算1/5/10/20日IC,看衰减曲线。
    """
    if factors is None:
        factors = ["mass_zscore","momentum_5","momentum_20","volatility_20","turnover_20"]
    periods = [p for p in [1,5,10,20] if p <= max_period]
    results = []
    close_panel = None
    from . import momentum
    for name in factors:
        if name.startswith("momentum"):
            p = momentum.compute_momentum_panel(db_path, int(name.split("_")[1]))
        elif name.startswith("volatility"):
            p = momentum.compute_volatility_panel(db_path, int(name.split("_")[1]))
        elif name.startswith("turnover"):
            p = momentum.compute_turnover_panel(db_path, int(name.split("_")[1]))
        elif name in ("mass_zscore","mass_neu","mass_raw"):
            p = storage.load_factor_panel(db_path, name)
        else:
            continue
        if p.empty:
            continue
        if close_panel is None:
            close_panel = storage.load_close_panel(db_path, p.index[0], p.index[-1])
        common = p.index.intersection(close_panel.index).tolist()
        if len(common) < 3:
            continue
        fwd = compute_forward_returns(close_panel.loc[common], periods)
        ic = compute_ic_series(p.loc[common], fwd)
        row = {"factor": name}
        for pp in periods:
            summary = next((s for s in summarize_ic(ic) if s["forward_days"] == pp), {})
            row[f"ic_{pp}"] = summary.get("ic_mean")
            row[f"ir_{pp}"] = summary.get("ir")
        # 半衰期估计: IC降到一半的周期(粗略)
        ic1 = row.get("ic_1") or row.get("ic_5")
        if ic1:
            half = None
            for pp in periods:
                v = row.get(f"ic_{pp}")
                if v is not None and abs(v) < abs(ic1) / 2:
                    half = pp
                    break
            row["half_life"] = half
        results.append(row)
    return results
