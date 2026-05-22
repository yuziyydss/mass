#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

"""
跟踪高盛相关主体在 A 股定期报告披露口径中的持仓。

说明：
- 数据来自 Tushare 前十大股东/前十大流通股东接口。
- 这不是实时全仓，只能反映上市公司定期报告披露的股东名单。
"""

import argparse
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence, Tuple

import numpy as np
import pandas as pd
import tushare as ts
from tqdm import tqdm

TOKEN_ENV_KEYS = ("TUSHARE_TOKEN", "TS_TOKEN", "TUSHARE_PRO_TOKEN")
DEFAULT_KEYWORDS = ("高盛国际", "高盛公司", "高盛亚洲", "高盛（亚洲）", "GOLDMAN", "GOLDMAN SACHS")
LOGGER = logging.getLogger("goldman_tracker")

HOLDER_APIS = {
    "float": ("top10_floatholders", "前十大流通股东"),
    "all": ("top10_holders", "前十大股东"),
}

OUTPUT_COLUMNS = [
    "period",
    "source",
    "code",
    "name",
    "industry",
    "ann_date",
    "end_date",
    "holder_name",
    "holder_key",
    "hold_amount",
    "hold_ratio",
    "matched_keyword",
]


@dataclass(frozen=True)
class TrackerConfig:
    retries: int = 3
    retry_sleep_seconds: float = 2.0
    request_sleep_seconds: float = 0.15
    page_size: int = 6000
    max_pages: int = 20


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def _parse_env_value(raw_value: str) -> str:
    value = raw_value.strip()
    if not value:
        return ""
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1].strip()
    if "#" in value:
        value = value.split("#", 1)[0].strip()
    return value


def load_token_from_env_file(env_file: Path) -> Optional[str]:
    if not env_file.exists() or not env_file.is_file():
        return None

    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        if key.strip() not in TOKEN_ENV_KEYS:
            continue
        value = _parse_env_value(raw_value)
        if value:
            return value
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
    try:
        return ts.pro_api(token=token)
    except TypeError:
        ts.set_token(token)
        return ts.pro_api()


def normalize_holder_name(name: object) -> str:
    if pd.isna(name):
        return ""
    return str(name).upper().replace(" ", "").replace("　", "").replace("-", "").replace("－", "")


def match_holder(name: object, keywords: Sequence[str]) -> str:
    normalized_name = normalize_holder_name(name)
    for keyword in keywords:
        normalized_keyword = normalize_holder_name(keyword)
        if normalized_keyword and normalized_keyword in normalized_name:
            return keyword
    return ""


def latest_disclosed_period(reference_date: Optional[str] = None) -> str:
    """按常见定报披露节奏估算最近一期：5/1 后 Q1，9/1 后中报，11/1 后 Q3。"""
    ref = datetime.strptime(reference_date, "%Y%m%d") if reference_date else datetime.now()
    y = ref.year
    md = ref.month * 100 + ref.day

    if md >= 1101:
        return f"{y}0930"
    if md >= 901:
        return f"{y}0630"
    if md >= 501:
        return f"{y}0331"
    return f"{y - 1}0930"


def previous_period(period: str) -> str:
    year = int(period[:4])
    mmdd = period[4:]
    order = ["0331", "0630", "0930", "1231"]
    if mmdd not in order:
        raise ValueError("period 必须是 YYYY0331/YYYY0630/YYYY0930/YYYY1231")

    idx = order.index(mmdd)
    if idx == 0:
        return f"{year - 1}1231"
    return f"{year}{order[idx - 1]}"


def load_stock_list(pro, codes: Optional[Sequence[str]] = None, max_stocks: Optional[int] = None) -> pd.DataFrame:
    if codes:
        unique_codes = list(dict.fromkeys(code.strip() for code in codes if code.strip()))
        stock = pro.stock_basic(exchange="", list_status="L", fields="ts_code,name,industry")
        stock = stock.rename(columns={"ts_code": "code"})
        stock = stock[stock["code"].isin(unique_codes)]
        missing = sorted(set(unique_codes) - set(stock["code"]))
        if missing:
            LOGGER.warning("以下代码不在当前上市股票列表中: %s", ",".join(missing))
        return stock[["code", "name", "industry"]]

    stock = pro.stock_basic(exchange="", list_status="L", fields="ts_code,name,industry")
    if stock is None or stock.empty:
        raise RuntimeError("未获取到股票列表")
    stock = stock.rename(columns={"ts_code": "code"})[["code", "name", "industry"]]
    if max_stocks:
        stock = stock.head(max_stocks)
    return stock


def normalize_holder_df(
    raw: pd.DataFrame,
    stock_row: pd.Series,
    period: str,
    source: str,
    keywords: Sequence[str],
) -> pd.DataFrame:
    if raw is None or raw.empty or "holder_name" not in raw.columns:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    df = raw.copy()
    df["matched_keyword"] = df["holder_name"].map(lambda x: match_holder(x, keywords))
    df = df[df["matched_keyword"] != ""]
    if df.empty:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    if "hold_amount" not in df.columns:
        df["hold_amount"] = np.nan
    if "hold_ratio" not in df.columns:
        ratio_cols = [col for col in df.columns if "ratio" in col.lower()]
        df["hold_ratio"] = df[ratio_cols[0]] if ratio_cols else np.nan

    df["period"] = period
    df["source"] = source
    df["code"] = stock_row["code"]
    df["name"] = stock_row.get("name")
    df["industry"] = stock_row.get("industry")
    df["holder_key"] = df["holder_name"].map(normalize_holder_name)
    df["hold_amount"] = pd.to_numeric(df["hold_amount"], errors="coerce")
    df["hold_ratio"] = pd.to_numeric(df["hold_ratio"], errors="coerce")

    for col in ["ann_date", "end_date"]:
        if col not in df.columns:
            df[col] = ""

    return df.reindex(columns=OUTPUT_COLUMNS)


def normalize_bulk_holder_df(
    raw: pd.DataFrame,
    stock_list: pd.DataFrame,
    period: str,
    source: str,
    keywords: Sequence[str],
) -> pd.DataFrame:
    if raw is None or raw.empty or "holder_name" not in raw.columns:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    df = raw.copy()
    if "ts_code" in df.columns:
        df = df.rename(columns={"ts_code": "code"})
    if "code" not in df.columns:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    df["matched_keyword"] = df["holder_name"].map(lambda x: match_holder(x, keywords))
    df = df[df["matched_keyword"] != ""]
    if df.empty:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    if "hold_amount" not in df.columns:
        df["hold_amount"] = np.nan
    if "hold_ratio" not in df.columns:
        ratio_cols = [col for col in df.columns if "ratio" in col.lower()]
        df["hold_ratio"] = df[ratio_cols[0]] if ratio_cols else np.nan

    df = df.merge(stock_list[["code", "name", "industry"]], on="code", how="left")
    df["period"] = period
    df["source"] = source
    df["holder_key"] = df["holder_name"].map(normalize_holder_name)
    df["hold_amount"] = pd.to_numeric(df["hold_amount"], errors="coerce")
    df["hold_ratio"] = pd.to_numeric(df["hold_ratio"], errors="coerce")

    for col in ["ann_date", "end_date"]:
        if col not in df.columns:
            df[col] = ""

    return df.reindex(columns=OUTPUT_COLUMNS)


def call_holder_api(pro, api_name: str, code: str, period: str, cfg: TrackerConfig) -> pd.DataFrame:
    api = getattr(pro, api_name)
    for attempt in range(cfg.retries):
        try:
            return api(ts_code=code, period=period)
        except Exception as err:
            LOGGER.warning("%s %s 查询失败(%s/%s): %s", api_name, code, attempt + 1, cfg.retries, err)
            if attempt < cfg.retries - 1:
                time.sleep(cfg.retry_sleep_seconds)
    return pd.DataFrame()


def call_holder_api_page(
    pro,
    api_name: str,
    period: str,
    limit: int,
    offset: int,
    cfg: TrackerConfig,
) -> pd.DataFrame:
    api = getattr(pro, api_name)
    for attempt in range(cfg.retries):
        try:
            return api(period=period, limit=limit, offset=offset)
        except Exception as err:
            LOGGER.warning(
                "%s period=%s offset=%s 查询失败(%s/%s): %s",
                api_name,
                period,
                offset,
                attempt + 1,
                cfg.retries,
                err,
            )
            if attempt < cfg.retries - 1:
                time.sleep(cfg.retry_sleep_seconds)
    return pd.DataFrame()


def read_processed_codes(processed_file: Path) -> set[str]:
    if not processed_file.exists():
        return set()
    return {line.strip() for line in processed_file.read_text(encoding="utf-8").splitlines() if line.strip()}


def append_csv(df: pd.DataFrame, path: Path) -> None:
    if df.empty:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, mode="a", header=not path.exists(), index=False, encoding="utf-8-sig")


def fetch_period_matches(
    pro,
    stock_list: pd.DataFrame,
    period: str,
    sources: Sequence[str],
    output_dir: Path,
    keywords: Sequence[str],
    cfg: TrackerConfig,
) -> pd.DataFrame:
    period_parts = []

    for source in sources:
        api_name, source_label = HOLDER_APIS[source]
        result_file = output_dir / f"goldman_positions_{period}_{source}.csv"
        processed_file = output_dir / ".progress" / f"processed_{period}_{source}.txt"
        processed_codes = read_processed_codes(processed_file)

        LOGGER.info("%s %s: 已处理 %s / %s", period, source_label, len(processed_codes), len(stock_list))
        processed_file.parent.mkdir(parents=True, exist_ok=True)

        for row in tqdm(stock_list.itertuples(index=False), total=len(stock_list), desc=f"{period}-{source}"):
            code = row.code
            if code in processed_codes:
                continue

            raw = call_holder_api(pro, api_name, code, period, cfg)
            matched = normalize_holder_df(raw, pd.Series(row._asdict()), period, source, keywords)
            append_csv(matched, result_file)

            with processed_file.open("a", encoding="utf-8") as fh:
                fh.write(f"{code}\n")

            time.sleep(cfg.request_sleep_seconds)

        if result_file.exists():
            period_parts.append(pd.read_csv(result_file, encoding="utf-8-sig"))

    if not period_parts:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    result = pd.concat(period_parts, ignore_index=True)
    result = result.drop_duplicates(subset=["period", "source", "code", "holder_key"], keep="last")
    result = result.reindex(columns=OUTPUT_COLUMNS)
    combined_file = output_dir / f"goldman_positions_{period}.csv"
    result.to_csv(combined_file, index=False, encoding="utf-8-sig")
    LOGGER.info("已保存 %s 期高盛持仓: %s 条 -> %s", period, len(result), combined_file)
    return result


def fetch_period_matches_bulk(
    pro,
    stock_list: pd.DataFrame,
    period: str,
    sources: Sequence[str],
    output_dir: Path,
    keywords: Sequence[str],
    cfg: TrackerConfig,
    refresh: bool = False,
) -> pd.DataFrame:
    period_parts = []
    stock_codes = set(stock_list["code"])

    for source in sources:
        api_name, source_label = HOLDER_APIS[source]
        result_file = output_dir / f"goldman_positions_{period}_{source}.csv"

        if result_file.exists() and not refresh:
            LOGGER.info("读取已有结果: %s", result_file)
            period_parts.append(pd.read_csv(result_file, encoding="utf-8-sig"))
            continue

        pages = []
        LOGGER.info("批量拉取 %s %s", period, source_label)
        for page in range(cfg.max_pages):
            offset = page * cfg.page_size
            raw = call_holder_api_page(pro, api_name, period, cfg.page_size, offset, cfg)
            if raw is None or raw.empty:
                break

            pages.append(raw)
            LOGGER.info("%s %s offset=%s rows=%s", period, source_label, offset, len(raw))
            if len(raw) < cfg.page_size:
                break
            time.sleep(cfg.request_sleep_seconds)

        if not pages:
            matched = pd.DataFrame(columns=OUTPUT_COLUMNS)
        else:
            raw_all = pd.concat(pages, ignore_index=True)
            raw_all = raw_all.drop_duplicates()
            matched = normalize_bulk_holder_df(raw_all, stock_list, period, source, keywords)
            matched = matched[matched["code"].isin(stock_codes)]
            matched = matched.drop_duplicates(subset=["period", "source", "code", "holder_key"], keep="last")

        result_file.parent.mkdir(parents=True, exist_ok=True)
        matched.to_csv(result_file, index=False, encoding="utf-8-sig")
        LOGGER.info("已保存 %s %s 匹配结果: %s 条 -> %s", period, source_label, len(matched), result_file)
        period_parts.append(matched)

    if not period_parts:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    result = pd.concat(period_parts, ignore_index=True)
    result = result.drop_duplicates(subset=["period", "source", "code", "holder_key"], keep="last")
    combined_file = output_dir / f"goldman_positions_{period}.csv"
    result.to_csv(combined_file, index=False, encoding="utf-8-sig")
    LOGGER.info("已保存 %s 期高盛持仓汇总: %s 条 -> %s", period, len(result), combined_file)
    return result


def build_change_report(current: pd.DataFrame, previous: pd.DataFrame) -> pd.DataFrame:
    key_cols = ["source", "code", "holder_key"]
    left_cols = key_cols + ["period", "name", "industry", "holder_name", "hold_amount", "hold_ratio"]
    prev_cols = key_cols + ["period", "hold_amount", "hold_ratio"]

    cur = current.reindex(columns=left_cols).rename(
        columns={
            "period": "current_period",
            "hold_amount": "current_hold_amount",
            "hold_ratio": "current_hold_ratio",
        }
    )
    pre = previous.reindex(columns=prev_cols).rename(
        columns={
            "period": "previous_period",
            "hold_amount": "previous_hold_amount",
            "hold_ratio": "previous_hold_ratio",
        }
    )

    merged = cur.merge(pre, on=key_cols, how="outer")
    merged["amount_change"] = merged["current_hold_amount"].fillna(0) - merged["previous_hold_amount"].fillna(0)
    merged["ratio_change"] = merged["current_hold_ratio"].fillna(0) - merged["previous_hold_ratio"].fillna(0)

    conditions = [
        merged["previous_hold_amount"].isna() & merged["current_hold_amount"].notna(),
        merged["current_hold_amount"].isna() & merged["previous_hold_amount"].notna(),
        merged["amount_change"] > 0,
        merged["amount_change"] < 0,
    ]
    choices = ["新进", "退出", "增持", "减持"]
    merged["change_type"] = np.select(conditions, choices, default="不变")

    return merged.sort_values(["change_type", "amount_change"], ascending=[True, False])


def parse_sources(source_arg: str) -> list[str]:
    if source_arg == "both":
        return ["float", "all"]
    return [source_arg]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="跟踪高盛最新 A 股持仓（定期报告披露口径）")
    parser.add_argument("--period", default=None, help="报告期，如 20260331；不传则按当前日期估算最新披露期")
    parser.add_argument("--previous-period", default=None, help="对比报告期；不传则自动取上一报告期")
    parser.add_argument("--date", default=None, help="用于估算最新披露期的日期，格式 YYYYMMDD")
    parser.add_argument("--source", choices=["float", "all", "both"], default="both", help="查询口径")
    parser.add_argument("--mode", choices=["auto", "bulk", "per-stock"], default="auto", help="查询模式，默认自动选择")
    parser.add_argument("--codes", default=None, help="只查询指定股票代码，多个用逗号分隔")
    parser.add_argument("--max-stocks", type=int, default=None, help="最多查询前 N 只股票，调试用")
    parser.add_argument("--output", default="factors/goldman", help="输出目录")
    parser.add_argument("--keywords", default=",".join(DEFAULT_KEYWORDS), help="匹配关键词，逗号分隔")
    parser.add_argument("--token", default=None, help="Tushare token；优先级最高")
    parser.add_argument("--env-file", default=".env", help="环境变量文件路径，默认 .env")
    parser.add_argument("--sleep", type=float, default=0.15, help="每次接口调用后的等待秒数")
    parser.add_argument("--retries", type=int, default=3, help="接口失败重试次数")
    parser.add_argument("--page-size", type=int, default=6000, help="批量模式每页条数")
    parser.add_argument("--max-pages", type=int, default=20, help="批量模式最多分页数")
    parser.add_argument("--refresh", action="store_true", help="忽略已有结果文件，重新拉取")
    parser.add_argument("--no-compare", action="store_true", help="只输出当前期持仓，不生成环比变化")
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    parser.add_argument("--self-test", action="store_true", help="运行本地自测，不访问网络")
    return parser.parse_args()


def run_self_test() -> int:
    assert latest_disclosed_period("20260516") == "20260331"
    assert previous_period("20260331") == "20251231"
    assert previous_period("20251231") == "20250930"
    assert match_holder("高盛国际-自有资金", DEFAULT_KEYWORDS) == "高盛国际"
    assert match_holder("高盛尔", DEFAULT_KEYWORDS) == ""
    assert match_holder("Goldman Sachs International", DEFAULT_KEYWORDS) in {"GOLDMAN", "GOLDMAN SACHS"}

    current = pd.DataFrame(
        [
            {"source": "float", "code": "000001.SZ", "holder_key": "高盛", "period": "20260331", "name": "A", "industry": "银行", "holder_name": "高盛", "hold_amount": 120.0, "hold_ratio": 1.2},
            {"source": "float", "code": "000002.SZ", "holder_key": "高盛", "period": "20260331", "name": "B", "industry": "地产", "holder_name": "高盛", "hold_amount": 30.0, "hold_ratio": 0.3},
        ]
    )
    previous = pd.DataFrame(
        [
            {"source": "float", "code": "000001.SZ", "holder_key": "高盛", "period": "20251231", "hold_amount": 100.0, "hold_ratio": 1.0},
            {"source": "float", "code": "000003.SZ", "holder_key": "高盛", "period": "20251231", "hold_amount": 80.0, "hold_ratio": 0.8},
        ]
    )
    report = build_change_report(current, previous)
    assert set(report["change_type"]) == {"增持", "新进", "退出"}
    print("SELF_TEST_OK")
    return 0


def main() -> int:
    args = parse_args()
    setup_logging(args.verbose)

    if args.self_test:
        return run_self_test()

    token, token_source = resolve_token(args.token, Path(args.env_file))
    if not token:
        LOGGER.error("未找到 Tushare token。请设置 %s，或传入 --token。", "/".join(TOKEN_ENV_KEYS))
        return 2

    period = args.period or latest_disclosed_period(args.date)
    prev_period = args.previous_period or previous_period(period)
    sources = parse_sources(args.source)
    keywords = [item.strip() for item in args.keywords.split(",") if item.strip()]
    codes = [item.strip() for item in args.codes.split(",")] if args.codes else None
    cfg = TrackerConfig(
        retries=args.retries,
        request_sleep_seconds=args.sleep,
        page_size=args.page_size,
        max_pages=args.max_pages,
    )
    output_dir = Path(args.output)
    mode = args.mode
    if mode == "auto":
        mode = "per-stock" if codes else "bulk"

    LOGGER.info("Token 来源: %s", token_source)
    LOGGER.info("当前报告期: %s，对比报告期: %s，口径: %s，模式: %s", period, prev_period, ",".join(sources), mode)

    pro = init_tushare_client(token)
    stock_list = load_stock_list(pro, codes=codes, max_stocks=args.max_stocks)
    LOGGER.info("待查询股票数: %s", len(stock_list))

    fetcher = fetch_period_matches if mode == "per-stock" else fetch_period_matches_bulk
    if mode == "per-stock":
        current = fetcher(pro, stock_list, period, sources, output_dir, keywords, cfg)
    else:
        current = fetcher(pro, stock_list, period, sources, output_dir, keywords, cfg, args.refresh)
    if args.no_compare:
        return 0

    if mode == "per-stock":
        previous = fetcher(pro, stock_list, prev_period, sources, output_dir, keywords, cfg)
    else:
        previous = fetcher(pro, stock_list, prev_period, sources, output_dir, keywords, cfg, args.refresh)
    change_report = build_change_report(current, previous)
    change_file = output_dir / f"goldman_changes_{prev_period}_{period}.csv"
    change_report.to_csv(change_file, index=False, encoding="utf-8-sig")
    LOGGER.info("已保存变化报告: %s 条 -> %s", len(change_report), change_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
