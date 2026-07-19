#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import pandas as pd


def check_mass_quality(df: pd.DataFrame, min_rows: int) -> list[tuple[str, str]]:
    alerts: list[tuple[str, str]] = []
    row_count = len(df)
    if row_count < min_rows:
        alerts.append(("WARN", f"样本数偏少：{row_count}，低于阈值 {min_rows}"))

    if row_count == 0:
        alerts.append(("ERROR", "本次 MASS 结果为空"))
        return alerts

    if "mass_zscore" in df.columns:
        null_ratio = float(df["mass_zscore"].isna().mean())
        if null_ratio > 0.05:
            alerts.append(("WARN", f"mass_zscore 缺失比例偏高：{null_ratio:.2%}"))

    if "total_mkt_cap" in df.columns:
        cap_null_ratio = float(df["total_mkt_cap"].isna().mean())
        if cap_null_ratio > 0.2:
            alerts.append(("WARN", f"市值缺失比例偏高：{cap_null_ratio:.2%}"))

    if "industry" in df.columns:
        industry_null_ratio = float(df["industry"].isna().mean())
        if industry_null_ratio > 0.2:
            alerts.append(("WARN", f"行业缺失比例偏高：{industry_null_ratio:.2%}"))

    # 估值字段检查：pb/dv_ratio 若全空说明 schema 未迁移成功或拉取失败
    for col, label in (("pb", "市净率PB"), ("dv_ratio", "股息率")):
        if col in df.columns:
            null_ratio = float(df[col].isna().mean())
            if null_ratio > 0.5:
                alerts.append(("WARN", f"{label} 缺失比例偏高：{null_ratio:.2%}"))
        else:
            # 列不存在：schema 迁移可能没生效
            alerts.append(("ERROR", f"{label} 字段缺失（factor_mass_daily 无 {col} 列，检查 schema 迁移）"))

    return alerts

