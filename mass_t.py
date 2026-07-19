#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
计算全市场 A 股的梅斯线（MASS）因子
数据源：Tushare Pro
步骤：获取行情 -> 计算 MASS -> 中位数去极值 -> 行业+市值对数中性化 -> zscore
"""
from __future__ import annotations

import argparse
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
import tushare as ts
from tqdm import tqdm

# MASS 参数
N1 = 9
N2 = 25
M = 6

OUTPUT_COLUMNS = [
    "code",
    "name",
    "industry",
    "total_mkt_cap",
    "pe",
    "pb",
    "dv_ratio",
    "mass_raw",
    "mass_clip",
    "mass_neu",
    "mass_zscore",
]
BASE_FILL_COLUMNS = ["name", "industry", "total_mkt_cap", "pe", "pb", "dv_ratio"]
RESUME_REQUIRED_COLUMNS = ["code", "mass_raw"]
TOKEN_ENV_KEYS = ("TUSHARE_TOKEN", "TS_TOKEN", "TUSHARE_PRO_TOKEN")

LOGGER = logging.getLogger("mass_factor")


@dataclass(frozen=True)
class RuntimeConfig:
    history_window_days: int = 200
    min_history_points: int = max(50, N2 + M + N1 + 5)
    max_retries: int = 3
    fetch_retry_sleep_seconds: float = 1.0
    market_cap_retry_sleep_seconds: float = 5.0
    request_sleep_seconds: float = 0.1
    industry_batch_size: int = 1000
    industry_batch_sleep_seconds: float = 1.0
    progress_save_every: int = 50
    market_cap_max_lookback_days: int = 10


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def _parse_env_value(raw_value: str) -> str:
    value = raw_value.strip()
    if not value:
        return ""

    # 支持带引号的 .env 值
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1].strip()

    # 非引号值支持行尾注释：TOKEN=xxx # comment
    if "#" in value:
        value = value.split("#", 1)[0].strip()
    return value


def load_token_from_env_file(env_file: Path) -> Optional[str]:
    if not env_file.exists() or not env_file.is_file():
        return None

    try:
        for raw_line in env_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue

            if line.startswith("export "):
                line = line[7:].strip()

            if "=" not in line:
                continue

            key, raw_value = line.split("=", 1)
            key = key.strip()
            if key not in TOKEN_ENV_KEYS:
                continue

            value = _parse_env_value(raw_value)
            if value:
                return value
    except Exception as err:
        LOGGER.warning("读取 .env 文件失败 %s: %s", env_file, err)
        return None

    return None


def resolve_token(cli_token: Optional[str], env_file: Path) -> Tuple[Optional[str], str]:
    if cli_token:
        return cli_token, "--token"

    for env_key in TOKEN_ENV_KEYS:
        value = os.getenv(env_key)
        if value:
            return value, f"env:{env_key}"

    file_token = load_token_from_env_file(env_file)
    if file_token:
        return file_token, f"file:{env_file}"

    return None, ""


def init_tushare_client(token: str):
    # 优先直接注入 token，避免 ts.set_token() 在受限环境写 tk.csv 失败
    try:
        return ts.pro_api(token=token)
    except TypeError:
        # 兼容旧版 tushare：不支持关键字参数时回退
        ts.set_token(token)
        return ts.pro_api()


def calc_mass_factor(df: Optional[pd.DataFrame]) -> Optional[float]:
    """按常见公式计算单只股票的 MASS 因子末值。"""
    required = N2 + M + N1 + 5
    if df is None or df.empty or len(df) < required:
        return None

    high = pd.to_numeric(df["high"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")
    hl = high - low
    ema1 = hl.ewm(span=N1, adjust=False).mean()
    ema2 = ema1.ewm(span=N1, adjust=False).mean()
    ratio = (ema1 / ema2).replace([np.inf, -np.inf], np.nan)
    mass = ratio.rolling(N2).sum()
    mass_m = mass.rolling(M).mean()
    val = mass_m.iloc[-1]
    return None if pd.isna(val) else float(val)


def fetch_history(pro, code: str, end_date: str, cfg: RuntimeConfig) -> Optional[pd.DataFrame]:
    """获取单只股票历史行情（end_date 向前取足够窗口）。"""
    end_dt = datetime.strptime(end_date, "%Y%m%d")
    start_dt = end_dt - timedelta(days=cfg.history_window_days)

    for attempt in range(cfg.max_retries):
        try:
            data = pro.daily(
                ts_code=code,
                start_date=start_dt.strftime("%Y%m%d"),
                end_date=end_dt.strftime("%Y%m%d"),
            )
            if data is None or data.empty:
                LOGGER.warning("股票 %s 未获取到数据", code)
                continue

            required_cols = {"high", "low", "trade_date"}
            missing_cols = sorted(required_cols - set(data.columns))
            if missing_cols:
                LOGGER.warning("股票 %s 缺少必要列: %s", code, missing_cols)
                continue

            data = data.sort_values("trade_date").reset_index(drop=True)
            if len(data) < cfg.min_history_points:
                LOGGER.warning("股票 %s 数据不足: %s 天", code, len(data))
                continue

            if end_date not in set(data["trade_date"].astype(str)):
                LOGGER.info("股票 %s 缺少 %s 当日数据，使用最近可得交易日", code, end_date)
            return data
        except Exception as err:
            LOGGER.warning("股票 %s 获取数据失败(%s/%s): %s", code, attempt + 1, cfg.max_retries, err)
            if attempt < cfg.max_retries - 1:
                time.sleep(cfg.fetch_retry_sleep_seconds)

    return None


def load_stock_list(pro) -> pd.DataFrame:
    """获取全 A 股代码与名称。"""
    data = pro.stock_basic(exchange="", list_status="L", fields="ts_code,name,market")
    if data is None or data.empty:
        raise RuntimeError("未获取到股票列表")

    LOGGER.info("股票数量: %s", len(data))
    return data[["ts_code", "name"]].rename(columns={"ts_code": "code"})


def get_previous_trade_date(pro, target_date: str) -> str:
    """获取目标日期之前的上一个交易日。"""
    try:
        target_dt = datetime.strptime(target_date, "%Y%m%d")
        start_date = (target_dt - timedelta(days=30)).strftime("%Y%m%d")
        cal_data = pro.trade_cal(exchange="SSE", start_date=start_date, end_date=target_date)

        if cal_data is None or cal_data.empty:
            fallback = (target_dt - timedelta(days=1)).strftime("%Y%m%d")
            LOGGER.warning("未获取到交易日历数据，回退到 %s", fallback)
            return fallback

        open_days = cal_data[cal_data["is_open"] == 1].sort_values("cal_date", ascending=False)
        previous = open_days[open_days["cal_date"] < target_date]
        if not previous.empty:
            return str(previous.iloc[0]["cal_date"])

        if not open_days.empty:
            return str(open_days.iloc[0]["cal_date"])

        fallback = (target_dt - timedelta(days=1)).strftime("%Y%m%d")
        LOGGER.warning("交易日历中无开市日期，回退到 %s", fallback)
        return fallback
    except Exception as err:
        target_dt = datetime.strptime(target_date, "%Y%m%d")
        fallback = (target_dt - timedelta(days=1)).strftime("%Y%m%d")
        LOGGER.warning("查找上个交易日失败: %s，回退到 %s", err, fallback)
        return fallback


def resolve_trade_date(pro, date_arg: Optional[str]) -> str:
    """解析交易日。未传 date 时默认取今天之前最近一个交易日。"""
    if date_arg:
        datetime.strptime(date_arg, "%Y%m%d")
        return date_arg

    today = datetime.now().strftime("%Y%m%d")
    resolved = get_previous_trade_date(pro, today)
    LOGGER.info("未指定 --date，自动使用最近交易日: %s", resolved)
    return resolved


def load_market_cap(pro, trade_date: str, cfg: RuntimeConfig) -> pd.DataFrame:
    """获取总市值和市盈率；若当日无数据则向前回溯交易日。"""
    current_date = trade_date

    for day_offset in range(cfg.market_cap_max_lookback_days):
        if day_offset > 0:
            current_date = get_previous_trade_date(pro, current_date)
            LOGGER.info("尝试回溯交易日 %s 获取市值数据", current_date)

        for attempt in range(cfg.max_retries):
            try:
                data = pro.daily_basic(
                    ts_code="",
                    trade_date=current_date,
                    fields="ts_code,total_mv,pe_ttm,pb,dv_ratio",
                )
                if data is not None and not data.empty:
                    data = data.rename(
                        columns={"ts_code": "code", "total_mv": "total_mkt_cap", "pe_ttm": "pe"}
                    )
                    data["total_mkt_cap"] = pd.to_numeric(data["total_mkt_cap"], errors="coerce")
                    data["pe"] = pd.to_numeric(data["pe"], errors="coerce")
                    data["pb"] = pd.to_numeric(data["pb"], errors="coerce")
                    data["dv_ratio"] = pd.to_numeric(data["dv_ratio"], errors="coerce")
                    LOGGER.info("成功获取 %s 条市值数据 (trade_date=%s)", len(data), current_date)
                    return data[["code", "total_mkt_cap", "pe", "pb", "dv_ratio"]]

                LOGGER.warning("trade_date=%s 市值数据为空", current_date)
                break
            except Exception as err:
                LOGGER.warning(
                    "获取市值数据失败(%s/%s, trade_date=%s): %s",
                    attempt + 1,
                    cfg.max_retries,
                    current_date,
                    err,
                )
                if attempt < cfg.max_retries - 1:
                    time.sleep(cfg.market_cap_retry_sleep_seconds)

    LOGGER.error("市值数据获取失败，返回空表")
    return pd.DataFrame(columns=["code", "total_mkt_cap", "pe", "pb", "dv_ratio"])


def read_industry_cache(cache_file: Path, requested_codes: Optional[set[str]] = None) -> pd.DataFrame:
    if not cache_file.exists():
        return pd.DataFrame(columns=["code", "industry"])

    try:
        cached = pd.read_pickle(cache_file)
        if cached is None or cached.empty:
            return pd.DataFrame(columns=["code", "industry"])

        if "code" not in cached.columns or "industry" not in cached.columns:
            LOGGER.warning("行业缓存字段不完整，忽略缓存: %s", cache_file)
            return pd.DataFrame(columns=["code", "industry"])

        cached = cached[["code", "industry"]].drop_duplicates(subset=["code"], keep="last")
        if requested_codes is None:
            return cached

        filtered = cached[cached["code"].isin(requested_codes)]
        if not filtered.empty:
            LOGGER.info("缓存命中行业映射: %s 条", len(filtered))
        return filtered
    except Exception as err:
        LOGGER.warning("读取行业缓存失败 %s: %s", cache_file, err)
        return pd.DataFrame(columns=["code", "industry"])


def load_wind_industry_map(
    pro,
    stock_codes: Sequence[str],
    cache_file: Path,
    cfg: RuntimeConfig,
) -> pd.DataFrame:
    """通过 stock_basic 批量获取行业映射（支持缓存与增量补齐）。"""
    requested_codes = set(stock_codes)
    if not requested_codes:
        return pd.DataFrame(columns=["code", "industry"])

    cached = read_industry_cache(cache_file, requested_codes)
    cached_codes = set(cached["code"].tolist()) if not cached.empty else set()
    remaining_codes = list(requested_codes - cached_codes)
    all_parts = [cached] if not cached.empty else []

    if remaining_codes:
        LOGGER.info("开始拉取行业映射，待补齐股票数: %s", len(remaining_codes))
        batch_size = cfg.industry_batch_size

        for i in range(0, len(remaining_codes), batch_size):
            batch_codes = remaining_codes[i : i + batch_size]
            LOGGER.info(
                "行业映射批次 %s (%s-%s)",
                i // batch_size + 1,
                i + 1,
                min(i + batch_size, len(remaining_codes)),
            )
            try:
                df_batch = pro.stock_basic(
                    ts_code=",".join(batch_codes),
                    list_status="L",
                    fields="ts_code,symbol,industry",
                )
                if df_batch is None or df_batch.empty:
                    LOGGER.warning("行业映射批次 %s 无数据", i // batch_size + 1)
                else:
                    df_batch = df_batch.rename(columns={"ts_code": "code"})
                    all_parts.append(df_batch[["code", "industry"]])
            except Exception as err:
                LOGGER.warning("行业映射批次 %s 拉取失败: %s", i // batch_size + 1, err)

            if i + batch_size < len(remaining_codes):
                time.sleep(cfg.industry_batch_sleep_seconds)

    if not all_parts:
        LOGGER.warning("行业映射获取失败，返回空表")
        return pd.DataFrame(columns=["code", "industry"])

    final_df = pd.concat(all_parts, ignore_index=True)
    final_df = final_df.drop_duplicates(subset=["code"], keep="last")
    final_df = final_df[final_df["code"].isin(requested_codes)][["code", "industry"]]

    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        existing_cache = read_industry_cache(cache_file, None)
        if existing_cache.empty:
            cache_to_write = final_df
        else:
            cache_to_write = pd.concat([existing_cache, final_df], ignore_index=True)
            cache_to_write = cache_to_write.drop_duplicates(subset=["code"], keep="last")
        cache_to_write.to_pickle(cache_file)
    except Exception as err:
        LOGGER.warning("写入行业缓存失败 %s: %s", cache_file, err)

    LOGGER.info("行业映射就绪: %s 条", len(final_df))
    return final_df


def median_mad_clip(series: pd.Series, k: float = 5.0) -> pd.Series:
    """中位数去极值（MAD）。"""
    med = series.median()
    mad = (series - med).abs().median()
    if mad == 0 or np.isnan(mad):
        return series
    z = (series - med) / (1.4826 * mad)
    z = z.clip(-k, k)
    return med + z * 1.4826 * mad


def neutralize(df: pd.DataFrame, factor_col: str, industry_col: str, size_col: str) -> pd.Series:
    """
    行业 + 市值对数中性化。
    如果行业数据为空，则只做市值对数中性化。
    """
    tmp = df[[factor_col, size_col]].copy()
    tmp[size_col] = pd.to_numeric(tmp[size_col], errors="coerce")
    tmp = tmp.dropna(subset=[factor_col, size_col])
    tmp = tmp[tmp[size_col] > 0]
    if tmp.empty:
        return pd.Series(index=df.index, dtype=float)

    has_industry = industry_col in df.columns and not df[industry_col].isna().all()
    if has_industry:
        industry_data = df[industry_col].dropna()
        common_index = tmp.index.intersection(industry_data.index)
        tmp = tmp.loc[common_index].copy()
        if tmp.empty:
            has_industry = False
        else:
            tmp[industry_col] = industry_data.loc[common_index].values

    if tmp.empty:
        return pd.Series(index=df.index, dtype=float)

    tmp["log_size"] = np.log(tmp[size_col])

    if has_industry:
        ind_dum = pd.get_dummies(tmp[industry_col], dummy_na=True)
        X = pd.concat([ind_dum, tmp[["log_size"]]], axis=1)
    else:
        X = tmp[["log_size"]]

    X.insert(0, "const", 1.0)
    y = tmp[factor_col].values.astype(float)
    X_mat = X.values.astype(float)

    try:
        coef, _, _, _ = np.linalg.lstsq(X_mat, y, rcond=None)
        fitted = X_mat @ coef
        tmp["resid"] = y - fitted
    except Exception as err:
        LOGGER.warning("中性化计算失败，使用原始值: %s", err)
        tmp["resid"] = y

    out = pd.Series(index=df.index, dtype=float)
    out.loc[tmp.index] = tmp["resid"]
    return out


def zscore(series: pd.Series) -> pd.Series:
    mu = series.mean()
    sigma = series.std(ddof=0)
    if sigma == 0 or np.isnan(sigma):
        return series * 0
    return (series - mu) / sigma


def fill_missing_from_base(df: pd.DataFrame, base: pd.DataFrame) -> pd.DataFrame:
    base_cols = ["code"] + BASE_FILL_COLUMNS
    merged = df.merge(base[base_cols], on="code", how="left", suffixes=("", "_base"))

    for col in BASE_FILL_COLUMNS:
        backup_col = f"{col}_base"
        if col in merged.columns and backup_col in merged.columns:
            merged[col] = merged[col].fillna(merged[backup_col])
        elif backup_col in merged.columns:
            merged[col] = merged[backup_col]
        merged = merged.drop(columns=[backup_col], errors="ignore")

    return merged


def normalize_resume_df(raw_df: pd.DataFrame, base: pd.DataFrame) -> pd.DataFrame:
    if raw_df is None or raw_df.empty:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    if not set(RESUME_REQUIRED_COLUMNS).issubset(raw_df.columns):
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    available_cols = [col for col in OUTPUT_COLUMNS if col in raw_df.columns]
    normalized = raw_df[available_cols].copy()
    normalized = normalized.dropna(subset=["code", "mass_raw"])
    if normalized.empty:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    normalized = fill_missing_from_base(normalized, base)
    normalized = normalized.drop_duplicates(subset=["code"], keep="last")
    return normalized


def load_resume_state(out_file: Path, progress_file: Path, base: pd.DataFrame) -> Tuple[set[str], list[dict]]:
    parts = []

    if out_file.exists():
        try:
            out_df = pd.read_csv(out_file, encoding="utf-8-sig")
            normalized = normalize_resume_df(out_df, base)
            if not normalized.empty:
                parts.append(normalized)
                LOGGER.info("从输出文件恢复记录: %s", len(normalized))
        except Exception as err:
            LOGGER.warning("读取输出文件失败 %s: %s", out_file, err)

    if progress_file.exists():
        try:
            progress_df = pd.read_pickle(progress_file)
            normalized = normalize_resume_df(progress_df, base)
            if not normalized.empty:
                parts.append(normalized)
                LOGGER.info("从进度文件恢复记录: %s", len(normalized))
        except Exception as err:
            LOGGER.warning("读取进度文件失败 %s: %s", progress_file, err)

    if not parts:
        return set(), []

    combined = pd.concat(parts, ignore_index=True)
    combined = combined.drop_duplicates(subset=["code"], keep="last")
    return set(combined["code"].tolist()), combined.to_dict("records")


def save_progress(rows: list[dict], progress_file: Path) -> None:
    progress_df = pd.DataFrame(rows)
    progress_df.to_pickle(progress_file)


def run_mass_loop(
    pro,
    base_to_process: pd.DataFrame,
    trade_date: str,
    existing_rows: list[dict],
    existing_count: int,
    progress_file: Path,
    cfg: RuntimeConfig,
    progress_callback: Optional[Callable[[int, int, str, int], None]] = None,
) -> list[dict]:
    rows = existing_rows.copy()
    total = len(base_to_process)
    if total == 0:
        LOGGER.info("所有股票已处理完成，无需继续下载")
        return rows

    LOGGER.info("开始计算 MASS 因子，待处理股票: %s", total)
    for i, row in enumerate(tqdm(base_to_process.itertuples(index=False), total=total, desc="处理进度"), start=1):
        code = row.code
        try:
            hist = fetch_history(pro, code, trade_date, cfg)
            val = calc_mass_factor(hist)
            rows.append(
                {
                    "code": code,
                    "name": row.name,
                    "industry": getattr(row, "industry", None),
                    "total_mkt_cap": getattr(row, "total_mkt_cap", None),
                    "pe": getattr(row, "pe", None),
                    "pb": getattr(row, "pb", None),
                    "dv_ratio": getattr(row, "dv_ratio", None),
                    "mass_raw": val,
                }
            )
        except Exception as err:
            LOGGER.warning("处理股票 %s 出错，已跳过: %s", code, err)

        if i % cfg.progress_save_every == 0:
            save_progress(rows, progress_file)
            LOGGER.info("进度已保存: %s/%s (累计已处理: %s)", len(rows), total + existing_count, existing_count + i)
            if progress_callback:
                progress_callback(existing_count + i, total + existing_count, code, len(rows))

        time.sleep(cfg.request_sleep_seconds)

    if progress_callback:
        progress_callback(total + existing_count, total + existing_count, "", len(rows))
    return rows


def post_process(rows: list[dict], base: pd.DataFrame) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    df = df.drop_duplicates(subset=["code"], keep="last")
    if df.empty:
        LOGGER.error("没有有效的数据，程序退出")
        return df

    total_stocks = len(df)
    null_mass_count = int(df["mass_raw"].isna().sum())
    valid_mass_count = total_stocks - null_mass_count
    LOGGER.info("数据统计: 总数=%s, 有效mass_raw=%s, 空mass_raw=%s", total_stocks, valid_mass_count, null_mass_count)

    df = df.dropna(subset=["mass_raw"])
    if df.empty:
        LOGGER.error("所有股票的 mass_raw 都为空，程序退出")
        return df

    df = fill_missing_from_base(df, base)
    LOGGER.info("补齐后市盈率缺失数量: %s", int(df["pe"].isna().sum()))

    df["mass_clip"] = median_mad_clip(df["mass_raw"])
    df["mass_neu"] = neutralize(df, "mass_clip", "industry", "total_mkt_cap")
    df["mass_zscore"] = zscore(df["mass_neu"])
    return df


def save_final_result(df: pd.DataFrame, out_file: Path, progress_file: Path) -> None:
    out_file.parent.mkdir(parents=True, exist_ok=True)
    df.reindex(columns=OUTPUT_COLUMNS).to_csv(out_file, index=False, encoding="utf-8-sig")
    LOGGER.info("已保存最终结果: %s，样本量: %s", out_file, len(df))

    if progress_file.exists():
        progress_file.unlink()
        LOGGER.info("已清理进度文件: %s", progress_file)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="计算全市场 A 股 MASS 因子")
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="交易日，格式 YYYYMMDD；不传时自动取最近交易日",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="factors",
        help="输出目录，默认 factors/",
    )
    parser.add_argument(
        "--token",
        type=str,
        default=None,
        help="Tushare token；优先级最高",
    )
    parser.add_argument(
        "--env-file",
        type=str,
        default=".env",
        help="环境变量文件路径，默认 .env",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="输出调试日志",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    setup_logging(args.verbose)

    token, token_source = resolve_token(args.token, Path(args.env_file))
    if not token:
        LOGGER.error(
            "未提供 Tushare token。请使用 --token，或设置环境变量 %s，或在 %s 中配置。",
            "/".join(TOKEN_ENV_KEYS),
            args.env_file,
        )
        return 2

    cfg = RuntimeConfig()
    LOGGER.info("Token 来源: %s", token_source)
    pro = init_tushare_client(token)

    try:
        trade_date = resolve_trade_date(pro, args.date)
    except ValueError:
        LOGGER.error("参数 --date 格式错误，应为 YYYYMMDD")
        return 2

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_file = output_dir / f"mass_{trade_date}.csv"
    progress_file = output_dir / f"progress_{trade_date}.pkl"
    cache_file = output_dir / "industry_cache.pkl"

    LOGGER.info("目标交易日: %s", trade_date)
    stock_list = load_stock_list(pro)
    mkt_cap = load_market_cap(pro, trade_date, cfg)

    stock_codes = stock_list["code"].tolist()
    industry_map = load_wind_industry_map(pro, stock_codes, cache_file, cfg)
    if industry_map.empty:
        LOGGER.warning("行业映射为空，将仅做市值中性化")
        industry_map = pd.DataFrame(columns=["code", "industry"])

    base = stock_list.merge(industry_map, on="code", how="left")
    base = base.merge(mkt_cap, on="code", how="left")
    LOGGER.info(
        "基础数据规模=%s, 有市盈率=%s, 有总市值=%s",
        len(base),
        int(base["pe"].notna().sum()),
        int(base["total_mkt_cap"].notna().sum()),
    )

    existing_codes, existing_rows = load_resume_state(out_file, progress_file, base)
    base_to_process = base[~base["code"].isin(existing_codes)].copy()
    if existing_codes:
        LOGGER.info("续跑恢复: 已处理=%s, 待处理=%s", len(existing_codes), len(base_to_process))

    rows = run_mass_loop(
        pro=pro,
        base_to_process=base_to_process,
        trade_date=trade_date,
        existing_rows=existing_rows,
        existing_count=len(existing_codes),
        progress_file=progress_file,
        cfg=cfg,
    )

    final_df = post_process(rows, base)
    if final_df.empty:
        return 1

    save_final_result(final_df, out_file, progress_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
