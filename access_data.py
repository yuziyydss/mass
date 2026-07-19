"""下载指定股票的日行情、资产负债表和利润表到 CSV。"""

import os
from pathlib import Path

import pandas as pd
import tushare as ts
from dotenv import load_dotenv


DATA_DIR = Path("data")
START_DATE = "20230101"
END_DATE = "20250905"
FIN_END_DATE = "20251231"

# ts_code 列表
STOCK_CODES = {
    "600519.SH": "600519",
    "000681.SZ": "000681",
    "688324.BJ": "688324",
    "300364.SZ": "300364",
}


def init_client() -> ts.pro_api:
    load_dotenv()
    token = os.getenv("TUSHARE_TOKEN") or os.getenv("TS_TOKEN")
    if not token:
        raise RuntimeError("未找到 TUSHARE_TOKEN，请检查 .env")
    ts.set_token(token)
    return ts.pro_api()


def fetch_daily(pro: ts.pro_api, ts_code: str) -> pd.DataFrame:
    df = pro.daily(
        ts_code=ts_code,
        start_date=START_DATE,
        end_date=END_DATE,
        fields="trade_date,ts_code,open,high,low,close,vol",
    )
    # 按日期升序便于阅读
    return df.sort_values("trade_date")


def fetch_balance_sheet(pro: ts.pro_api, ts_code: str) -> pd.DataFrame:
    """资产负债表（需要相应接口权限）。"""
    return pro.balancesheet(
        ts_code=ts_code,
        start_date=START_DATE,
        end_date=FIN_END_DATE,
    )


def fetch_income(pro: ts.pro_api, ts_code: str) -> pd.DataFrame:
    """利润表（需要相应接口权限）。"""
    return pro.income(
        ts_code=ts_code,
        start_date=START_DATE,
        end_date=FIN_END_DATE,
    )


def save_csv(df: pd.DataFrame, filename: Path) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    df.to_csv(DATA_DIR / filename, index=False, encoding="utf-8")


def main() -> None:
    pro = init_client()

    for ts_code, short in STOCK_CODES.items():
        print(f"开始处理 {ts_code} ...")
        # 日行情：普通账号一般都有权限
        daily = fetch_daily(pro, ts_code)
        save_csv(daily, Path(f"{short}_daily.csv"))
        print(f"  日行情已保存：{short}_daily.csv")

        # 资产负债表、利润表：需要高级权限，没权限时捕获异常
        try:
            balance = fetch_balance_sheet(pro, ts_code)
            save_csv(balance, Path(f"{short}_balancesheet_2023_2025.csv"))
            print(f"  资产负债表已保存：{short}_balancesheet_2023_2025.csv")
        except Exception as e:
            print(f"  资产负债表获取失败（可能无接口权限）：{e}")

        try:
            income = fetch_income(pro, ts_code)
            save_csv(income, Path(f"{short}_income_2023_2025.csv"))
            print(f"  利润表已保存：{short}_income_2023_2025.csv")
        except Exception as e:
            print(f"  利润表获取失败（可能无接口权限）：{e}")

        print(f"{ts_code} 处理完成\n")


if __name__ == "__main__":
    main()

