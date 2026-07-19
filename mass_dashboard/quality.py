#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

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
            alerts.append(("ERROR", f"{label} 字段缺失（factor_mass_daily 无 {col} 列，检查 schema 迁移）"))

    return alerts


def check_data_freshness(db_path: Path, max_stale_days: int = 3) -> Optional[tuple[str, str]]:
    """检查 MASS 数据是否陈旧。返回 (level, msg) 或 None。

    最新 factor_mass_daily 交易日距今超过 max_stale_days 天则告警。
    """
    from . import storage
    latest = storage.latest_trade_date(db_path)
    if not latest:
        return ("ERROR", "factor_mass_daily 表为空，无任何 MASS 结果")
    try:
        latest_dt = datetime.strptime(latest, "%Y%m%d")
    except ValueError:
        return ("WARN", f"最新交易日格式异常: {latest}")
    today = datetime.now()
    # 用自然日比较（交易日历不可得时粗略）
    stale_days = (today - latest_dt).days
    if stale_days > max_stale_days:
        return ("WARN", f"MASS 数据陈旧：最新交易日 {latest}，距今 {stale_days} 天（>{max_stale_days}）")
    return None




def db_integrity(db_path) -> dict:
    """数据库完整性检查：各表行数、最新日期、缺失统计。"""
    from . import storage
    result = {"tables": {}, "alerts": []}
    with storage._read_conn(db_path) as conn:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()]
        for t in tables:
            try:
                n = conn.execute(f"SELECT COUNT(*) as n FROM {t}").fetchone()["n"]
                latest = None
                if "trade_date" in [c[1] for c in conn.execute(f"PRAGMA table_info({t})").fetchall()]:
                    row = conn.execute(f"SELECT MAX(trade_date) AS d FROM {t}").fetchone()
                    latest = row["d"] if row else None
                result["tables"][t] = {"rows": n, "latest_date": latest}
            except Exception:
                pass
    # 告警
    if result["tables"].get("factor_mass_daily", {}).get("rows", 0) == 0:
        result["alerts"].append(("ERROR", "factor_mass_daily 表为空"))
    if result["tables"].get("daily_bars", {}).get("rows", 0) == 0:
        result["alerts"].append(("ERROR", "daily_bars 表为空"))
    return result


def next_run_time(config) -> dict:
    """预测下次调度执行时间。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(config.timezone)
    now = datetime.now(tz)
    hour, minute = [int(x) for x in config.run_time.split(":", 1)]
    # 今天的调度时间
    today_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < today_run:
        next_dt = today_run
    else:
        # 明天
        from datetime import timedelta
        next_dt = today_run + timedelta(days=1)
    return {
        "next_run": next_dt.strftime("%Y-%m-%d %H:%M %Z"),
        "run_time": config.run_time,
        "timezone": config.timezone,
        "hours_until": round((next_dt - now).total_seconds() / 3600, 1),
    }
