#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""财务指标按需拉取（个股详情页用）。

不走每日流水线（fina_indicator 按 ts_code 拉，无法批量按日，逐股拉 5000 次太慢）。
改为：个股详情页请求时按需拉单股最新财务指标，带内存缓存。
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import pandas as pd

import mass_t

LOGGER = logging.getLogger("mass_dashboard.financial")

# 内存缓存：code -> (timestamp, data)，5分钟过期
_cache: dict = {}
_CACHE_TTL = 300


def fetch_financial(pro, code: str) -> dict:
    """拉取单股最新财务指标：ROE / 净利润同比 / 营收同比 / 毛利率 / 净利率。"""
    now = time.time()
    cached = _cache.get(code)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]

    try:
        df = pro.fina_indicator(
            ts_code=code,
            fields="ts_code,end_date,roe,netprofit_margin,grossprofit_margin,q_profit_yoy,or_yoy",
        )
        if df is None or df.empty:
            result = {}
        else:
            df = df.sort_values("end_date", ascending=False).head(1)
            r = df.iloc[0]
            result = {
                "end_date": str(r.get("end_date", "")),
                "roe": _to_float(r.get("roe")),
                "netprofit_margin": _to_float(r.get("netprofit_margin")),
                "grossprofit_margin": _to_float(r.get("grossprofit_margin")),
                "q_profit_yoy": _to_float(r.get("q_profit_yoy")),
                "or_yoy": _to_float(r.get("or_yoy")),
            }
        _cache[code] = (now, result)
        return result
    except Exception as err:
        LOGGER.warning("拉取财务指标失败 %s: %s", code, err)
        return {}


def _to_float(v) -> Optional[float]:
    if v is None or pd.isna(v):
        return None
    try:
        return round(float(v), 4)
    except (TypeError, ValueError):
        return None
