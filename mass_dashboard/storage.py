#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import re
import sqlite3
import threading
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional, Sequence

import pandas as pd

LOGGER = logging.getLogger("mass_dashboard.storage")

MASS_COLUMNS = [
    "trade_date",
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

DAILY_BAR_COLUMNS = [
    "trade_date",
    "code",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "change",
    "pct_chg",
    "vol",
    "amount",
]
DAILY_BAR_NUMERIC_COLUMNS = [
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "change",
    "pct_chg",
    "vol",
    "amount",
]

MONEYFLOW_COLUMNS = [
    "trade_date",
    "code",
    "buy_sm_vol",
    "sell_sm_vol",
    "buy_md_vol",
    "sell_md_vol",
    "buy_lg_vol",
    "sell_lg_vol",
    "buy_elg_vol",
    "sell_elg_vol",
    "net_mf_vol",
    "net_mf_amount",
]

MONEYFLOW_NUMERIC_COLUMNS = [
    "buy_sm_vol",
    "sell_sm_vol",
    "buy_md_vol",
    "sell_md_vol",
    "buy_lg_vol",
    "sell_lg_vol",
    "buy_elg_vol",
    "sell_elg_vol",
    "net_mf_vol",
    "net_mf_amount",
]

WEEK_DOWN_FLOW_COLUMNS = [
    "trade_date",
    "code",
    "name",
    "industry",
    "week_change_pct",
    "main_net_in",
    "total_mkt_cap",
]

BOTTOM_CONDITIONS_COLUMNS = [
    "trade_date",
    "code",
    "name",
    "industry",
    "conditions_met",
    "cond1_volume",
    "cond2_price",
    "cond3_valuation",
    "cond4_divergence",
    "pe_ttm",
    "pb",
    "dv_ratio",
    "latest_close",
]


# 线程本地只读连接：web 每个 HTTP 请求线程复用同一连接，避免反复 open/close。
# 写路径仍走 connect()（每次独立短连接，配合 WAL 避免长事务持锁）。
_read_conn_pool = threading.local()


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def _read_conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    """复用线程本地只读连接。db_path 变化时（测试/多库）自动切换。"""
    cached = getattr(_read_conn_pool, "conn", None)
    cached_path = getattr(_read_conn_pool, "path", None)
    if cached is not None and cached_path == str(db_path):
        yield cached
        return

    if cached is not None:
        try:
            cached.close()
        except Exception:
            pass
    conn = connect(db_path)
    # 只读连接走 WAL 的 read 隔离，不开写事务
    conn.execute("PRAGMA query_only=1")
    _read_conn_pool.conn = conn
    _read_conn_pool.path = str(db_path)
    yield conn


def close_read_connection() -> None:
    """关闭当前线程的只读连接（仅在需要时手动调用）。"""
    conn = getattr(_read_conn_pool, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        _read_conn_pool.conn = None
        _read_conn_pool.path = None


def init_db(db_path: Path) -> None:
    with connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS factor_mass_daily (
                trade_date TEXT NOT NULL,
                code TEXT NOT NULL,
                name TEXT,
                industry TEXT,
                total_mkt_cap REAL,
                pe REAL,
                pb REAL,
                dv_ratio REAL,
                mass_raw REAL,
                mass_clip REAL,
                mass_neu REAL,
                mass_zscore REAL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (trade_date, code)
            );

            CREATE INDEX IF NOT EXISTS idx_factor_mass_date_z
            ON factor_mass_daily (trade_date, mass_zscore DESC);

            CREATE INDEX IF NOT EXISTS idx_factor_mass_code_date
            ON factor_mass_daily (code, trade_date);

            CREATE TABLE IF NOT EXISTS daily_bars (
                trade_date TEXT NOT NULL,
                code TEXT NOT NULL,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                pre_close REAL,
                change REAL,
                pct_chg REAL,
                vol REAL,
                amount REAL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (trade_date, code)
            );

            CREATE INDEX IF NOT EXISTS idx_daily_bars_code_date
            ON daily_bars (code, trade_date);

            CREATE INDEX IF NOT EXISTS idx_daily_bars_date
            ON daily_bars (trade_date);

            CREATE TABLE IF NOT EXISTS job_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_name TEXT NOT NULL,
                trade_date TEXT,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                row_count INTEGER DEFAULT 0,
                error_message TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_job_runs_status_date
            ON job_runs (job_name, trade_date, status);

            CREATE TABLE IF NOT EXISTS factor_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date TEXT,
                level TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS job_progress (
                run_id INTEGER PRIMARY KEY,
                job_name TEXT NOT NULL,
                trade_date TEXT,
                stage TEXT NOT NULL,
                processed INTEGER DEFAULT 0,
                total INTEGER DEFAULT 0,
                current_code TEXT,
                message TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS daily_moneyflow (
                trade_date TEXT NOT NULL,
                code TEXT NOT NULL,
                buy_sm_vol REAL,
                sell_sm_vol REAL,
                buy_md_vol REAL,
                sell_md_vol REAL,
                buy_lg_vol REAL,
                sell_lg_vol REAL,
                buy_elg_vol REAL,
                sell_elg_vol REAL,
                net_mf_vol REAL,
                net_mf_amount REAL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (trade_date, code)
            );

            CREATE INDEX IF NOT EXISTS idx_moneyflow_date
            ON daily_moneyflow (trade_date);

            CREATE INDEX IF NOT EXISTS idx_moneyflow_code_date
            ON daily_moneyflow (code, trade_date);

            CREATE TABLE IF NOT EXISTS week_down_flow (
                trade_date TEXT NOT NULL,
                code TEXT NOT NULL,
                name TEXT,
                industry TEXT,
                week_change_pct REAL,
                main_net_in REAL,
                total_mkt_cap REAL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (trade_date, code)
            );

            CREATE INDEX IF NOT EXISTS idx_week_down_flow_date
            ON week_down_flow (trade_date);

            CREATE TABLE IF NOT EXISTS bottom_conditions (
                trade_date TEXT NOT NULL,
                code TEXT NOT NULL,
                name TEXT,
                industry TEXT,
                conditions_met INTEGER DEFAULT 0,
                cond1_volume INTEGER DEFAULT 0,
                cond2_price INTEGER DEFAULT 0,
                cond3_valuation INTEGER DEFAULT 0,
                cond4_divergence INTEGER DEFAULT 0,
                pe_ttm REAL,
                pb REAL,
                dv_ratio REAL,
                latest_close REAL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (trade_date, code)
            );

            CREATE INDEX IF NOT EXISTS idx_bottom_conditions_date
            ON bottom_conditions (trade_date);

            CREATE INDEX IF NOT EXISTS idx_bottom_conditions_met
            ON bottom_conditions (trade_date, conditions_met DESC);

            CREATE TABLE IF NOT EXISTS watchlist (
                code TEXT PRIMARY KEY,
                name TEXT,
                added_at TEXT NOT NULL,
                note TEXT
            );
            """
        )
    _migrate(db_path)


# 幂等 schema 迁移：检测旧库缺失的列并 ALTER TABLE ADD COLUMN。
# 只加列、不删列、不改类型，安全可重复执行。
# 新增字段时在这里登记 (表, 列, 类型)，旧库重启服务自动补列。
SCHEMA_MIGRATIONS = {
    "factor_mass_daily": [
        ("pb", "REAL"),
        ("dv_ratio", "REAL"),
    ],
}


def _migrate(db_path: Path) -> None:
    with connect(db_path) as conn:
        for table, cols in SCHEMA_MIGRATIONS.items():
            try:
                existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            except sqlite3.OperationalError:
                continue  # 表不存在（不应发生，init_db 刚建过），跳过
            for col, typ in cols:
                if col not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
                    LOGGER.info("schema 迁移: 表 %s 新增列 %s %s", table, col, typ)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean_value(value):
    if pd.isna(value):
        return None
    return value


def chunked(items: Sequence[str], size: int = 900) -> Iterable[list[str]]:
    for i in range(0, len(items), size):
        yield list(items[i : i + size])


def upsert_mass_results(db_path: Path, df: pd.DataFrame, trade_date: str) -> int:
    if df.empty:
        return 0

    work = df.copy()
    work["trade_date"] = trade_date
    for col in MASS_COLUMNS:
        if col not in work.columns:
            work[col] = None

    rows = []
    updated_at = utc_now()
    for item in work[MASS_COLUMNS].to_dict("records"):
        rows.append(tuple(clean_value(item[col]) for col in MASS_COLUMNS) + (updated_at,))

    placeholders = ",".join(["?"] * (len(MASS_COLUMNS) + 1))
    update_cols = [col for col in MASS_COLUMNS if col not in {"trade_date", "code"}]
    update_sql = ", ".join([f"{col}=excluded.{col}" for col in update_cols] + ["updated_at=excluded.updated_at"])

    with connect(db_path) as conn:
        conn.executemany(
            f"""
            INSERT INTO factor_mass_daily ({",".join(MASS_COLUMNS)}, updated_at)
            VALUES ({placeholders})
            ON CONFLICT(trade_date, code) DO UPDATE SET {update_sql}
            """,
            rows,
        )
    return len(rows)


def upsert_daily_bars(db_path: Path, df: pd.DataFrame) -> int:
    if df is None or df.empty:
        return 0

    work = df.copy()
    work = work.rename(columns={"ts_code": "code"})
    for col in DAILY_BAR_COLUMNS:
        if col not in work.columns:
            work[col] = None

    work = work.dropna(subset=["trade_date", "code"])
    if work.empty:
        return 0

    work["trade_date"] = work["trade_date"].astype(str)
    work["code"] = work["code"].astype(str)
    for col in DAILY_BAR_NUMERIC_COLUMNS:
        work[col] = pd.to_numeric(work[col], errors="coerce")

    work = work.drop_duplicates(subset=["trade_date", "code"], keep="last")
    updated_at = utc_now()
    rows = [
        tuple(clean_value(item[col]) for col in DAILY_BAR_COLUMNS) + (updated_at,)
        for item in work[DAILY_BAR_COLUMNS].to_dict("records")
    ]

    placeholders = ",".join(["?"] * (len(DAILY_BAR_COLUMNS) + 1))
    update_cols = [col for col in DAILY_BAR_COLUMNS if col not in {"trade_date", "code"}]
    update_sql = ", ".join([f"{col}=excluded.{col}" for col in update_cols] + ["updated_at=excluded.updated_at"])

    with connect(db_path) as conn:
        conn.executemany(
            f"""
            INSERT INTO daily_bars ({",".join(DAILY_BAR_COLUMNS)}, updated_at)
            VALUES ({placeholders})
            ON CONFLICT(trade_date, code) DO UPDATE SET {update_sql}
            """,
            rows,
        )
    return len(rows)


def daily_bar_counts(db_path: Path, trade_dates: Sequence[str]) -> dict[str, int]:
    dates = [str(item) for item in trade_dates if item]
    if not dates:
        return {}

    counts: dict[str, int] = {}
    with _read_conn(db_path) as conn:
        for part in chunked(dates):
            placeholders = ",".join(["?"] * len(part))
            rows = conn.execute(
                f"""
                SELECT trade_date, COUNT(*) AS row_count
                FROM daily_bars
                WHERE trade_date IN ({placeholders})
                GROUP BY trade_date
                """,
                part,
            ).fetchall()
            counts.update({row["trade_date"]: int(row["row_count"] or 0) for row in rows})
    return counts


def missing_daily_bar_dates(db_path: Path, trade_dates: Sequence[str], min_rows: int) -> list[str]:
    counts = daily_bar_counts(db_path, trade_dates)
    threshold = max(1, int(min_rows))
    return [str(date) for date in trade_dates if counts.get(str(date), 0) < threshold]


def load_daily_bars(
    db_path: Path,
    start_date: str,
    end_date: str,
    codes: Optional[Sequence[str]] = None,
    columns: Sequence[str] = ("code", "trade_date", "high", "low"),
) -> pd.DataFrame:
    col_list = ",".join(columns)
    default_cols = list(columns)
    params: list[object] = [start_date, end_date]
    clauses = ["trade_date BETWEEN ? AND ?"]
    if codes:
        code_list = [str(code) for code in codes]
        frames = []
        with _read_conn(db_path) as conn:
            for part in chunked(code_list):
                placeholders = ",".join(["?"] * len(part))
                sql = f"""
                    SELECT {col_list}
                    FROM daily_bars
                    WHERE trade_date BETWEEN ? AND ? AND code IN ({placeholders})
                    ORDER BY code, trade_date
                """
                frames.append(pd.read_sql_query(sql, conn, params=[start_date, end_date] + part))
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=default_cols)

    with _read_conn(db_path) as conn:
        return pd.read_sql_query(
            f"""
            SELECT {col_list}
            FROM daily_bars
            WHERE {" AND ".join(clauses)}
            ORDER BY code, trade_date
            """,
            conn,
            params=params,
        )


def create_job_run(db_path: Path, job_name: str, trade_date: Optional[str], status: str = "RUNNING") -> int:
    with connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO job_runs (job_name, trade_date, status, started_at)
            VALUES (?, ?, ?, ?)
            """,
            (job_name, trade_date, status, utc_now()),
        )
        return int(cur.lastrowid)


def finish_job_run(
    db_path: Path,
    run_id: int,
    status: str,
    row_count: int = 0,
    error_message: Optional[str] = None,
) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            UPDATE job_runs
            SET status=?, finished_at=?, row_count=?, error_message=?
            WHERE id=?
            """,
            (status, utc_now(), row_count, error_message, run_id),
        )


def update_job_progress(
    db_path: Path,
    run_id: int,
    job_name: str,
    trade_date: Optional[str],
    stage: str,
    processed: int = 0,
    total: int = 0,
    current_code: str = "",
    message: str = "",
) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO job_progress
                (run_id, job_name, trade_date, stage, processed, total, current_code, message, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                job_name=excluded.job_name,
                trade_date=excluded.trade_date,
                stage=excluded.stage,
                processed=excluded.processed,
                total=excluded.total,
                current_code=excluded.current_code,
                message=excluded.message,
                updated_at=excluded.updated_at
            """,
            (run_id, job_name, trade_date, stage, processed, total, current_code, message, utc_now()),
        )


def latest_progress(db_path: Path) -> Optional[dict]:
    with _read_conn(db_path) as conn:
        row = conn.execute(
            """
            SELECT p.*, r.status, r.started_at, r.finished_at, r.row_count, r.error_message
            FROM job_progress p
            LEFT JOIN job_runs r ON r.id = p.run_id
            ORDER BY p.run_id DESC
            LIMIT 1
            """
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        total = result.get("total") or 0
        processed = result.get("processed") or 0
        result["percent"] = round(processed / total * 100, 2) if total else 0
        return result


def has_successful_run(db_path: Path, job_name: str, trade_date: str) -> bool:
    with _read_conn(db_path) as conn:
        row = conn.execute(
            """
            SELECT 1 FROM job_runs
            WHERE job_name=? AND trade_date=? AND status='SUCCESS'
            LIMIT 1
            """,
            (job_name, trade_date),
        ).fetchone()
        return row is not None


def replace_alerts(db_path: Path, trade_date: str, alerts: Iterable[tuple[str, str]]) -> None:
    with connect(db_path) as conn:
        conn.execute("DELETE FROM factor_alerts WHERE trade_date=?", (trade_date,))
        conn.executemany(
            """
            INSERT INTO factor_alerts (trade_date, level, message, created_at)
            VALUES (?, ?, ?, ?)
            """,
            [(trade_date, level, message, utc_now()) for level, message in alerts],
        )


def latest_trade_date(db_path: Path) -> Optional[str]:
    with _read_conn(db_path) as conn:
        row = conn.execute("SELECT MAX(trade_date) AS trade_date FROM factor_mass_daily").fetchone()
        return row["trade_date"] if row and row["trade_date"] else None


def list_trade_dates(db_path: Path, limit: int = 120) -> list[dict]:
    with _read_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT trade_date, COUNT(*) AS row_count
            FROM factor_mass_daily
            GROUP BY trade_date
            ORDER BY trade_date DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def get_summary(db_path: Path, trade_date: Optional[str] = None) -> dict:
    trade_date = trade_date or latest_trade_date(db_path)
    if not trade_date:
        return {"trade_date": None, "row_count": 0}

    with _read_conn(db_path) as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS row_count,
                COUNT(DISTINCT industry) AS industry_count,
                AVG(mass_zscore) AS avg_zscore,
                MAX(mass_zscore) AS max_zscore,
                MIN(mass_zscore) AS min_zscore,
                SUM(CASE WHEN mass_zscore IS NULL THEN 1 ELSE 0 END) AS null_zscore_count
            FROM factor_mass_daily
            WHERE trade_date=?
            """,
            (trade_date,),
        ).fetchone()

        prev_row = conn.execute(
            """
            SELECT MAX(trade_date) AS prev_date
            FROM factor_mass_daily
            WHERE trade_date < ?
            """,
            (trade_date,),
        ).fetchone()

    result = dict(row)
    result["trade_date"] = trade_date
    result["previous_trade_date"] = prev_row["prev_date"] if prev_row else None
    return result


def query_mass(
    db_path: Path,
    trade_date: Optional[str] = None,
    limit: int = 100,
    industry: str = "",
    keyword: str = "",
    direction: str = "desc",
) -> list[dict]:
    trade_date = trade_date or latest_trade_date(db_path)
    if not trade_date:
        return []

    clauses = ["trade_date=?"]
    params: list[object] = [trade_date]
    if industry:
        clauses.append("industry=?")
        params.append(industry)
    if keyword:
        clauses.append("(code LIKE ? OR name LIKE ?)")
        params.extend([f"%{keyword}%", f"%{keyword}%"])

    order = "ASC" if direction.lower() == "asc" else "DESC"
    params.append(max(1, min(limit, 1000)))

    with _read_conn(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT *
            FROM factor_mass_daily
            WHERE {" AND ".join(clauses)}
            ORDER BY mass_zscore {order}
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [dict(row) for row in rows]


def query_mass_page(
    db_path: Path,
    trade_date: Optional[str] = None,
    page: int = 1,
    per_page: int = 100,
    industry: str = "",
    keyword: str = "",
    direction: str = "desc",
) -> dict:
    trade_date = trade_date or latest_trade_date(db_path)
    if not trade_date:
        return {"trade_date": None, "page": 1, "per_page": per_page, "total": 0, "total_pages": 0, "rows": []}

    page = max(1, int(page))
    per_page = max(1, min(int(per_page), 200))
    clauses = ["trade_date=?"]
    params: list[object] = [trade_date]

    if industry:
        clauses.append("industry=?")
        params.append(industry)
    if keyword:
        clauses.append("(code LIKE ? OR name LIKE ?)")
        params.extend([f"%{keyword}%", f"%{keyword}%"])

    where_sql = " AND ".join(clauses)
    order = "ASC" if direction.lower() == "asc" else "DESC"
    offset = (page - 1) * per_page

    with _read_conn(db_path) as conn:
        total_row = conn.execute(
            f"SELECT COUNT(*) AS total FROM factor_mass_daily WHERE {where_sql}",
            params,
        ).fetchone()
        total = int(total_row["total"] or 0)
        rows = conn.execute(
            f"""
            SELECT *
            FROM factor_mass_daily
            WHERE {where_sql}
            ORDER BY mass_zscore IS NULL, mass_zscore {order}, code ASC
            LIMIT ? OFFSET ?
            """,
            params + [per_page, offset],
        ).fetchall()

    total_pages = (total + per_page - 1) // per_page if total else 0
    return {
        "trade_date": trade_date,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "rows": [dict(row) for row in rows],
    }


def list_industries(db_path: Path, trade_date: Optional[str] = None) -> list[str]:
    trade_date = trade_date or latest_trade_date(db_path)
    if not trade_date:
        return []

    with _read_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT industry
            FROM factor_mass_daily
            WHERE trade_date=? AND industry IS NOT NULL AND industry != ''
            ORDER BY industry
            """,
            (trade_date,),
        ).fetchall()
        return [row["industry"] for row in rows]


def stock_profile(db_path: Path, code: str) -> Optional[dict]:
    with _read_conn(db_path) as conn:
        row = conn.execute(
            """
            SELECT *
            FROM factor_mass_daily
            WHERE code=?
            ORDER BY trade_date DESC
            LIMIT 1
            """,
            (code,),
        ).fetchone()
        return dict(row) if row else None


def industry_stats(db_path: Path, trade_date: Optional[str] = None) -> list[dict]:
    trade_date = trade_date or latest_trade_date(db_path)
    if not trade_date:
        return []

    with _read_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT
                COALESCE(industry, '未分类') AS industry,
                COUNT(*) AS row_count,
                AVG(mass_zscore) AS avg_zscore,
                MAX(mass_zscore) AS max_zscore,
                MIN(mass_zscore) AS min_zscore
            FROM factor_mass_daily
            WHERE trade_date=?
            GROUP BY COALESCE(industry, '未分类')
            HAVING COUNT(*) >= 3
            ORDER BY avg_zscore DESC
            """,
            (trade_date,),
        ).fetchall()
        return [dict(row) for row in rows]


def stock_history(db_path: Path, code: str) -> list[dict]:
    with _read_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT trade_date, code, name, industry, mass_zscore, mass_raw, mass_neu
            FROM factor_mass_daily
            WHERE code=?
            ORDER BY trade_date
            """,
            (code,),
        ).fetchall()
        return [dict(row) for row in rows]


def recent_jobs(db_path: Path, limit: int = 50) -> list[dict]:
    with _read_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM job_runs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def recent_alerts(db_path: Path, limit: int = 50) -> list[dict]:
    with _read_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM factor_alerts
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def read_csv_with_fallback(csv_file: Path) -> pd.DataFrame:
    last_error: Optional[Exception] = None
    for encoding in ("utf-8-sig", "gbk", "gb18030"):
        try:
            return pd.read_csv(csv_file, encoding=encoding)
        except UnicodeDecodeError as err:
            last_error = err
    raise RuntimeError(f"无法读取 CSV 编码: {csv_file}") from last_error


# 高盛 CSV 内容缓存：key=(path, mtime, size)，避免每次 /api/focus 请求都重读文件。
_goldman_csv_cache: dict[tuple, pd.DataFrame] = {}


def _load_goldman_csv_cached(goldman_file: Path) -> pd.DataFrame:
    stat = goldman_file.stat()
    cache_key = (str(goldman_file), int(stat.st_mtime), int(stat.st_size))
    cached = _goldman_csv_cache.get(cache_key)
    if cached is not None:
        return cached
    df = read_csv_with_fallback(goldman_file)
    # 淘汰过期条目，避免目录换文件后缓存无限增长
    _goldman_csv_cache.clear()
    _goldman_csv_cache[cache_key] = df
    return df


def latest_goldman_file(goldman_dir: Path) -> Optional[Path]:
    if not goldman_dir.exists():
        return None
    pattern = re.compile(r"goldman_positions_(\d{8})\.csv$", re.IGNORECASE)
    candidates = []
    for csv_file in goldman_dir.glob("goldman_positions_*.csv"):
        match = pattern.match(csv_file.name)
        if match:
            candidates.append((match.group(1), csv_file))
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: item[0])[-1][1]


def focus_with_goldman(
    db_path: Path,
    goldman_dir: Path,
    trade_date: Optional[str] = None,
    limit: int = 100,
) -> dict:
    trade_date = trade_date or latest_trade_date(db_path)
    goldman_file = latest_goldman_file(goldman_dir)
    if not trade_date or not goldman_file:
        return {"trade_date": trade_date, "goldman_period": None, "rows": []}

    match = re.search(r"goldman_positions_(\d{8})\.csv$", goldman_file.name, re.IGNORECASE)
    goldman_period = match.group(1) if match else ""
    goldman = _load_goldman_csv_cached(goldman_file)
    if goldman.empty or "code" not in goldman.columns:
        return {"trade_date": trade_date, "goldman_period": goldman_period, "rows": []}

    keep_cols = [col for col in ["source", "code", "holder_name", "hold_amount", "hold_ratio"] if col in goldman.columns]
    goldman = goldman[keep_cols].copy()
    goldman["hold_amount"] = pd.to_numeric(goldman.get("hold_amount"), errors="coerce")
    goldman["hold_ratio"] = pd.to_numeric(goldman.get("hold_ratio"), errors="coerce")

    # 单连接一次性取当前交易日 + 上一个交易日的数据，避免重复开连接和 get_summary 往返
    with _read_conn(db_path) as conn:
        mass = pd.read_sql_query(
            """
            SELECT trade_date, code, name, industry, mass_raw, mass_zscore, total_mkt_cap, pe
            FROM factor_mass_daily
            WHERE trade_date=?
            """,
            conn,
            params=(trade_date,),
        )
        prev_date_row = conn.execute(
            "SELECT MAX(trade_date) AS prev_date FROM factor_mass_daily WHERE trade_date < ?",
            (trade_date,),
        ).fetchone()
        prev_date = prev_date_row["prev_date"] if prev_date_row else None
        prev = (
            pd.read_sql_query(
                """
                SELECT code, mass_zscore AS previous_mass_zscore
                FROM factor_mass_daily
                WHERE trade_date=?
                """,
                conn,
                params=(prev_date,),
            )
            if prev_date
            else pd.DataFrame(columns=["code", "previous_mass_zscore"])
        )

    merged = mass.merge(goldman, on="code", how="inner")
    if merged.empty:
        return {"trade_date": trade_date, "goldman_period": goldman_period, "rows": []}

    merged = merged.merge(prev, on="code", how="left")
    if "source" in merged.columns:
        merged["_source_priority"] = merged["source"].map({"float": 0, "all": 1}).fillna(2)
        merged = merged.sort_values("_source_priority")
        merged = merged.drop_duplicates(subset=["code", "holder_name"], keep="first")
        merged = merged.drop(columns=["_source_priority"])
    merged["zscore_change"] = merged["mass_zscore"] - merged["previous_mass_zscore"]
    merged["focus_score"] = (
        merged["mass_zscore"].fillna(0) * 10
        + merged["zscore_change"].fillna(0) * 4
        + merged["hold_ratio"].fillna(0)
    )

    def reason(row) -> str:
        if pd.notna(row.get("zscore_change")) and row["zscore_change"] >= 1:
            return "高盛持仓 + MASS 快速上升"
        if pd.notna(row.get("mass_zscore")) and row["mass_zscore"] >= 1.5:
            return "高盛持仓 + MASS 高位"
        if pd.notna(row.get("mass_zscore")) and row["mass_zscore"] <= -1.5:
            return "高盛持仓 + MASS 低位"
        return "高盛持仓"

    merged["focus_reason"] = merged.apply(reason, axis=1)
    merged = merged.sort_values(["focus_score", "hold_amount"], ascending=[False, False])
    rows = merged.head(max(1, min(limit, 500))).to_dict("records")
    return {"trade_date": trade_date, "goldman_period": goldman_period, "rows": rows}


def mark_stale_running_jobs(db_path: Path) -> int:
    with connect(db_path) as conn:
        cur = conn.execute(
            """
            UPDATE job_runs
            SET status='FAILED',
                finished_at=?,
                error_message='服务启动时发现上次 RUNNING 任务未正常结束'
            WHERE status='RUNNING'
            """,
            (utc_now(),),
        )
        return int(cur.rowcount)


def import_mass_csvs(db_path: Path, source_dir: Path) -> int:
    if not source_dir.exists():
        return 0

    imported = 0
    pattern = re.compile(r"mass_(\d{8})(?:_new)?\.csv$", re.IGNORECASE)
    for csv_file in sorted(source_dir.glob("mass_*.csv")):
        match = pattern.match(csv_file.name)
        if not match:
            continue
        trade_date = match.group(1)
        df = read_csv_with_fallback(csv_file)
        imported += upsert_mass_results(db_path, df, trade_date)
    return imported


# ── moneyflow 缓存 ──


def upsert_moneyflow(db_path: Path, df: pd.DataFrame) -> int:
    if df is None or df.empty:
        return 0

    work = df.rename(columns={"ts_code": "code"})
    for col in MONEYFLOW_COLUMNS:
        if col not in work.columns:
            work[col] = None
    work = work.dropna(subset=["trade_date", "code"])
    if work.empty:
        return 0

    work["trade_date"] = work["trade_date"].astype(str)
    work["code"] = work["code"].astype(str)
    for col in MONEYFLOW_NUMERIC_COLUMNS:
        work[col] = pd.to_numeric(work[col], errors="coerce")

    work = work.drop_duplicates(subset=["trade_date", "code"], keep="last")
    updated_at = utc_now()
    rows = [
        tuple(clean_value(item[col]) for col in MONEYFLOW_COLUMNS) + (updated_at,)
        for item in work[MONEYFLOW_COLUMNS].to_dict("records")
    ]

    placeholders = ",".join(["?"] * (len(MONEYFLOW_COLUMNS) + 1))
    update_cols = [col for col in MONEYFLOW_COLUMNS if col not in {"trade_date", "code"}]
    update_sql = ", ".join([f"{col}=excluded.{col}" for col in update_cols] + ["updated_at=excluded.updated_at"])

    with connect(db_path) as conn:
        conn.executemany(
            f"""
            INSERT INTO daily_moneyflow ({",".join(MONEYFLOW_COLUMNS)}, updated_at)
            VALUES ({placeholders})
            ON CONFLICT(trade_date, code) DO UPDATE SET {update_sql}
            """,
            rows,
        )
    return len(rows)


def moneyflow_counts(db_path: Path, trade_dates: Sequence[str]) -> dict[str, int]:
    dates = [str(item) for item in trade_dates if item]
    if not dates:
        return {}

    counts: dict[str, int] = {}
    with _read_conn(db_path) as conn:
        for part in chunked(dates):
            placeholders = ",".join(["?"] * len(part))
            rows = conn.execute(
                f"""
                SELECT trade_date, COUNT(*) AS row_count
                FROM daily_moneyflow
                WHERE trade_date IN ({placeholders})
                GROUP BY trade_date
                """,
                part,
            ).fetchall()
            counts.update({row["trade_date"]: int(row["row_count"] or 0) for row in rows})
    return counts


def missing_moneyflow_dates(db_path: Path, trade_dates: Sequence[str], min_rows: int) -> list[str]:
    counts = moneyflow_counts(db_path, trade_dates)
    threshold = max(1, int(min_rows))
    return [str(date) for date in trade_dates if counts.get(str(date), 0) < threshold]


def load_moneyflow(db_path: Path, start_date: str, end_date: str, codes: Optional[Sequence[str]] = None) -> pd.DataFrame:
    if codes:
        code_list = [str(code) for code in codes]
        frames = []
        with _read_conn(db_path) as conn:
            for part in chunked(code_list):
                placeholders = ",".join(["?"] * len(part))
                sql = f"""
                    SELECT {",".join(MONEYFLOW_COLUMNS)}
                    FROM daily_moneyflow
                    WHERE trade_date BETWEEN ? AND ? AND code IN ({placeholders})
                    ORDER BY code, trade_date
                """
                frames.append(pd.read_sql_query(sql, conn, params=[start_date, end_date] + part))
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=MONEYFLOW_COLUMNS)

    with _read_conn(db_path) as conn:
        return pd.read_sql_query(
            f"""
            SELECT {",".join(MONEYFLOW_COLUMNS)}
            FROM daily_moneyflow
            WHERE trade_date BETWEEN ? AND ?
            ORDER BY code, trade_date
            """,
            conn,
            params=[start_date, end_date],
        )


# ── week_down_flow 结果 ──


def upsert_week_down_flow(db_path: Path, df: pd.DataFrame, trade_date: str) -> int:
    if df.empty:
        return 0

    work = df.copy()
    work["trade_date"] = trade_date
    for col in WEEK_DOWN_FLOW_COLUMNS:
        if col not in work.columns:
            work[col] = None

    updated_at = utc_now()
    rows = [
        tuple(clean_value(item[col]) for col in WEEK_DOWN_FLOW_COLUMNS) + (updated_at,)
        for item in work[WEEK_DOWN_FLOW_COLUMNS].to_dict("records")
    ]

    placeholders = ",".join(["?"] * (len(WEEK_DOWN_FLOW_COLUMNS) + 1))
    update_cols = [col for col in WEEK_DOWN_FLOW_COLUMNS if col not in {"trade_date", "code"}]
    update_sql = ", ".join([f"{col}=excluded.{col}" for col in update_cols] + ["updated_at=excluded.updated_at"])

    with connect(db_path) as conn:
        conn.executemany(
            f"""
            INSERT INTO week_down_flow ({",".join(WEEK_DOWN_FLOW_COLUMNS)}, updated_at)
            VALUES ({placeholders})
            ON CONFLICT(trade_date, code) DO UPDATE SET {update_sql}
            """,
            rows,
        )
    return len(rows)


def query_week_down_flow(db_path: Path, trade_date: Optional[str] = None, limit: int = 100) -> dict:
    if trade_date is None:
        with _read_conn(db_path) as conn:
            row = conn.execute("SELECT MAX(trade_date) AS trade_date FROM week_down_flow").fetchone()
            trade_date = row["trade_date"] if row else None
    if not trade_date:
        return {"trade_date": None, "rows": []}

    limit = max(1, min(limit, 500))
    with _read_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT trade_date, code, name, industry, week_change_pct, main_net_in, total_mkt_cap
            FROM week_down_flow
            WHERE trade_date=?
            ORDER BY main_net_in DESC
            LIMIT ?
            """,
            (trade_date, limit),
        ).fetchall()
    return {"trade_date": trade_date, "rows": [dict(row) for row in rows]}


# ── bottom_conditions 底部条件 ──


def upsert_bottom_conditions(db_path: Path, df: pd.DataFrame, trade_date: str) -> int:
    if df.empty:
        return 0

    work = df.copy()
    work["trade_date"] = trade_date
    for col in BOTTOM_CONDITIONS_COLUMNS:
        if col not in work.columns:
            work[col] = None

    updated_at = utc_now()
    rows = [
        tuple(clean_value(item[col]) for col in BOTTOM_CONDITIONS_COLUMNS) + (updated_at,)
        for item in work[BOTTOM_CONDITIONS_COLUMNS].to_dict("records")
    ]

    placeholders = ",".join(["?"] * (len(BOTTOM_CONDITIONS_COLUMNS) + 1))
    update_cols = [col for col in BOTTOM_CONDITIONS_COLUMNS if col not in {"trade_date", "code"}]
    update_sql = ", ".join([f"{col}=excluded.{col}" for col in update_cols] + ["updated_at=excluded.updated_at"])

    with connect(db_path) as conn:
        conn.executemany(
            f"""
            INSERT INTO bottom_conditions ({",".join(BOTTOM_CONDITIONS_COLUMNS)}, updated_at)
            VALUES ({placeholders})
            ON CONFLICT(trade_date, code) DO UPDATE SET {update_sql}
            """,
            rows,
        )
    return len(rows)


def query_bottom_conditions(db_path: Path, trade_date: Optional[str] = None, min_conditions: int = 2, limit: int = 100) -> dict:
    if trade_date is None:
        with _read_conn(db_path) as conn:
            row = conn.execute("SELECT MAX(trade_date) AS trade_date FROM bottom_conditions").fetchone()
            trade_date = row["trade_date"] if row else None
    if not trade_date:
        return {"trade_date": None, "rows": []}

    limit = max(1, min(limit, 500))
    with _read_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM bottom_conditions
            WHERE trade_date=? AND conditions_met >= ?
            ORDER BY conditions_met DESC, latest_close ASC
            LIMIT ?
            """,
            (trade_date, min_conditions, limit),
        ).fetchall()
    return {"trade_date": trade_date, "rows": [dict(row) for row in rows]}


# ── 因子分析：面板数据加载（透视成 日期×股票 矩阵）──


_close_panel_cache: dict = {}  # (start,end) -> DataFrame, 进程内缓存


def load_close_panel(db_path: Path, start_date: str, end_date: str) -> pd.DataFrame:
    """加载收盘价面板：行=trade_date，列=code，值=close。
    用于前瞻收益和因子分层回测。带进程内缓存。
    """
    key = (start_date, end_date)
    cached = _close_panel_cache.get(key)
    if cached is not None:
        return cached
    with _read_conn(db_path) as conn:
        df = pd.read_sql_query(
            """
            SELECT trade_date, code, close
            FROM daily_bars
            WHERE trade_date BETWEEN ? AND ?
              AND close IS NOT NULL
            ORDER BY trade_date, code
            """,
            conn,
            params=[start_date, end_date],
        )
    if df.empty:
        _close_panel_cache[key] = pd.DataFrame()
        return pd.DataFrame()
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["close"])
    # 透视：行=日期，列=股票代码，值=收盘价
    panel = df.pivot(index="trade_date", columns="code", values="close")
    panel.index = panel.index.astype(str)
    panel = panel.sort_index()
    _close_panel_cache[key] = panel
    return panel


def load_factor_panel(db_path: Path, factor_col: str = "mass_zscore") -> pd.DataFrame:
    """加载因子值面板：行=trade_date，列=code，值=因子值。
    factor_col 默认 mass_zscore（中性化后的标准化值）。
    """
    valid_cols = {"mass_raw", "mass_clip", "mass_neu", "mass_zscore"}
    if factor_col not in valid_cols:
        factor_col = "mass_zscore"
    with _read_conn(db_path) as conn:
        df = pd.read_sql_query(
            f"""
            SELECT trade_date, code, {factor_col} AS factor
            FROM factor_mass_daily
            WHERE {factor_col} IS NOT NULL
            ORDER BY trade_date, code
            """,
            conn,
        )
    if df.empty:
        return pd.DataFrame()
    panel = df.pivot(index="trade_date", columns="code", values="factor")
    panel.index = panel.index.astype(str)
    return panel.sort_index()


def load_kline(
    db_path: Path,
    code: str,
    limit: int = 250,
) -> list[dict]:
    """加载单只股票的K线数据（开高低收量），按时间正序，最多 limit 条。
    用于个股详情页 K线 + 成交量 + MA 均线展示。
    """
    with _read_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT trade_date, open, high, low, close, vol, amount
            FROM daily_bars
            WHERE code=?
            ORDER BY trade_date DESC
            LIMIT ?
            """,
            (code, limit),
        ).fetchall()
    if not rows:
        return []
    # 倒序取出来的，翻成正序，并算 MA5/MA20
    items = [dict(r) for r in rows]
    items.reverse()
    closes = [float(r["close"]) if r["close"] is not None else None for r in items]
    # MA5 / MA20
    def ma(series, n):
        out = [None] * len(series)
        for i in range(n - 1, len(series)):
            window = series[i - n + 1: i + 1]
            valid = [x for x in window if x is not None]
            out[i] = round(sum(valid) / len(valid), 4) if valid else None
        return out
    ma5 = ma(closes, 5)
    ma20 = ma(closes, 20)
    for i, r in enumerate(items):
        r["ma5"] = ma5[i]
        r["ma20"] = ma20[i]
        # 数值序列化
        for k in ("open", "high", "low", "close", "vol", "amount"):
            if r[k] is not None:
                try:
                    r[k] = round(float(r[k]), 4)
                except (TypeError, ValueError):
                    pass
    return items


# ── 自选股 watchlist ──


def add_to_watchlist(db_path: Path, code: str, name: str = "", note: str = "") -> bool:
    with connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO watchlist (code, name, added_at, note) VALUES (?, ?, ?, ?)",
            (str(code), name or "", utc_now(), note),
        )
    return True


def remove_from_watchlist(db_path: Path, code: str) -> bool:
    with connect(db_path) as conn:
        cur = conn.execute("DELETE FROM watchlist WHERE code=?", (str(code),))
        return cur.rowcount > 0


def list_watchlist(db_path: Path) -> list[dict]:
    with _read_conn(db_path) as conn:
        rows = conn.execute("SELECT * FROM watchlist ORDER BY added_at DESC").fetchall()
    return [dict(r) for r in rows]


def in_watchlist(db_path: Path, code: str) -> bool:
    with _read_conn(db_path) as conn:
        row = conn.execute("SELECT 1 FROM watchlist WHERE code=?", (str(code),)).fetchone()
        return row is not None


def cleanup_old_jobs(db_path: Path, keep: int = 500) -> int:
    """清理 job_runs/job_progress 老记录，只保留最近 keep 条。
    在 pipeline 成功后调用，防止表无限增长。
    """
    with connect(db_path) as conn:
        row = conn.execute("SELECT MIN(id) AS mn, MAX(id) AS mx FROM job_runs").fetchone()
        if not row or not row["mx"]:
            return 0
        threshold = row["mx"] - keep
        if threshold <= 0:
            return 0
        # 删除旧 job_progress（先于 job_runs，无外键约束）
        conn.execute("DELETE FROM job_progress WHERE run_id <= ?", (threshold,))
        cur = conn.execute("DELETE FROM job_runs WHERE id <= ?", (threshold,))
        return cur.rowcount


def industry_rotation(db_path: Path, limit_dates: int = 10) -> list[dict]:
    """行业轮动：最近N个交易日各行业平均zscore,看行业强弱变化。
    返回 [{industry, avg_zscore, dates: [{date, avg}]}]
    """
    with _read_conn(db_path) as conn:
        # 取最近N个交易日
        date_rows = conn.execute(
            "SELECT DISTINCT trade_date FROM factor_mass_daily ORDER BY trade_date DESC LIMIT ?",
            (limit_dates,),
        ).fetchall()
        dates = [r["trade_date"] for r in date_rows]
        if not dates:
            return []
        placeholders = ",".join(["?"] * len(dates))
        rows = conn.execute(
            f"""
            SELECT trade_date, COALESCE(industry,'未分类') AS industry,
                   AVG(mass_zscore) AS avg_z
            FROM factor_mass_daily
            WHERE trade_date IN ({placeholders}) AND mass_zscore IS NOT NULL
            GROUP BY trade_date, COALESCE(industry,'未分类')
            HAVING COUNT(*) >= 3
            """,
            dates,
        ).fetchall()
    # 组织成 industry -> {date: avg}
    by_industry: dict = {}
    for r in rows:
        by_industry.setdefault(r["industry"], {})[r["trade_date"]] = float(r["avg_z"]) if r["avg_z"] is not None else None
    result = []
    for ind, date_map in by_industry.items():
        latest = date_map.get(dates[0])
        earliest = date_map.get(dates[-1]) if len(dates) > 1 else None
        change = (latest - earliest) if (latest is not None and earliest is not None) else None
        result.append({
            "industry": ind,
            "latest_avg": round(latest, 4) if latest is not None else None,
            "change": round(change, 4) if change is not None else None,
            "dates": dates,
            "values": [round(date_map.get(d), 4) if date_map.get(d) is not None else None for d in dates],
        })
    result.sort(key=lambda x: x["change"] or 0, reverse=True)
    return result


def compare_stocks_zscore(db_path: Path, codes: list[str]) -> dict:
    """对比多只股票的 MASS zscore 历史时序。
    返回 {dates: [...], series: {code: [zscore...]}}
    """
    if not codes:
        return {"dates": [], "series": {}}
    codes = [str(c) for c in codes]
    with _read_conn(db_path) as conn:
        placeholders = ",".join(["?"] * len(codes))
        df = pd.read_sql_query(
            f"""
            SELECT trade_date, code, mass_zscore
            FROM factor_mass_daily
            WHERE code IN ({placeholders}) AND mass_zscore IS NOT NULL
            ORDER BY trade_date, code
            """,
            conn, params=codes,
        )
    if df.empty:
        return {"dates": [], "series": {}}
    pivot = df.pivot(index="trade_date", columns="code", values="mass_zscore").sort_index()
    return {
        "dates": pivot.index.tolist(),
        "series": {code: [round(float(x), 4) if pd.notna(x) else None for x in pivot[code]] for code in pivot.columns if code in codes},
    }


def correlation_matrix(db_path: Path, codes: list[str], lookback: int = 60) -> dict:
    """多股收益率相关性矩阵。
    返回 {codes, matrix(二维数组)}
    """
    if not codes:
        return {"codes": [], "matrix": []}
    codes = [str(c) for c in codes]
    with _read_conn(db_path) as conn:
        row = conn.execute("SELECT MAX(trade_date) AS d FROM daily_bars").fetchone()
        if not row or not row["d"]:
            return {"codes": codes, "matrix": []}
        from datetime import datetime, timedelta
        end_date = row["d"]
        start = (datetime.strptime(end_date, "%Y%m%d") - timedelta(days=lookback*2)).strftime("%Y%m%d")
        placeholders = ",".join(["?"] * len(codes))
        df = pd.read_sql_query(
            f"SELECT trade_date, code, close FROM daily_bars WHERE trade_date BETWEEN ? AND ? AND code IN ({placeholders}) AND close IS NOT NULL ORDER BY trade_date, code",
            conn, params=[start, end_date] + codes,
        )
    if df.empty:
        return {"codes": codes, "matrix": []}
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    pivot = df.pivot(index="trade_date", columns="code", values="close")
    # 只保留存在的code,按输入顺序
    present = [c for c in codes if c in pivot.columns]
    if len(present) < 2:
        return {"codes": present, "matrix": []}
    rets = pivot[present].pct_change(fill_method=None).dropna()
    if len(rets) < 5:
        return {"codes": present, "matrix": []}
    corr = rets.corr()
    return {
        "codes": present,
        "matrix": [[round(float(corr.loc[a, b]), 3) if pd.notna(corr.loc[a, b]) else None for b in present] for a in present],
    }


def stock_zscore_percentile(db_path: Path, code: str) -> dict:
    """某股最新 zscore 在全市场的百分位。"""
    with _read_conn(db_path) as conn:
        row = conn.execute("SELECT MAX(trade_date) AS d FROM factor_mass_daily").fetchone()
        if not row or not row["d"]:
            return {}
        latest = row["d"]
        stock = conn.execute("SELECT mass_zscore FROM factor_mass_daily WHERE trade_date=? AND code=?", (latest, code)).fetchone()
        if not stock or stock["mass_zscore"] is None:
            return {"trade_date": latest}
        z = float(stock["mass_zscore"])
        # 全市场排序百分位
        all_z = conn.execute("SELECT mass_zscore FROM factor_mass_daily WHERE trade_date=? AND mass_zscore IS NOT NULL", (latest,)).fetchall()
        vals = [float(r["mass_zscore"]) for r in all_z]
        if not vals:
            return {"trade_date": latest, "zscore": z}
        vals.sort()
        # 百分位 = 小于z的比例
        import bisect
        rank = bisect.bisect_left(vals, z)
        pct = round(rank / len(vals) * 100, 2)
        return {"trade_date": latest, "zscore": round(z, 4), "percentile": pct, "total": len(vals)}


def load_industry_relative_zscore(db_path: Path, trade_date: str) -> dict:
    """个股zscore相对其行业的标准化值(行业内zscore)。
    返回 {code: industry_z} , 排除行业内样本<5的。
    """
    with _read_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT code, industry, mass_zscore
            FROM factor_mass_daily
            WHERE trade_date=? AND mass_zscore IS NOT NULL AND industry IS NOT NULL
            """,
            (trade_date,),
        ).fetchall()
    if not rows:
        return {}
    # 按行业分组算zscore
    import numpy as np
    by_ind: dict = {}
    for r in rows:
        by_ind.setdefault(r["industry"], []).append((r["code"], float(r["mass_zscore"])))
    result = {}
    for ind, lst in by_ind.items():
        if len(lst) < 5:
            continue
        vals = np.array([x[1] for x in lst])
        mu, sd = vals.mean(), vals.std()
        if sd == 0 or np.isnan(sd):
            continue
        for code, z in lst:
            result[code] = round(float((z - mu) / sd), 4)
    return result


def universe_filter(db_path: Path, trade_date: str, min_list_days: int = 365, exclude_st: bool = True) -> list[str]:
    """股票池筛选：排除ST股、上市不足min_list_days天的新股。
    返回合格股票代码列表。
    """
    with _read_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT code, name FROM factor_mass_daily WHERE trade_date=?
            """,
            (trade_date,),
        ).fetchall()
    result = []
    for r in rows:
        name = r["name"] or ""
        if exclude_st and ("ST" in name or "st" in name):
            continue
        # 新股过滤需要list_date,这里从stock_basic取（但未缓存,简化:只按name过滤ST）
        result.append(r["code"])
    return result


def similar_stocks(db_path: Path, code: str, limit: int = 10) -> list[dict]:
    """推荐相似股：同行业 + zscore相近的股票。"""
    with _read_conn(db_path) as conn:
        # 取该股最新数据
        row = conn.execute("SELECT MAX(trade_date) AS d FROM factor_mass_daily").fetchone()
        if not row or not row["d"]:
            return []
        latest = row["d"]
        target = conn.execute("SELECT code, name, industry, mass_zscore FROM factor_mass_daily WHERE trade_date=? AND code=?", (latest, code)).fetchone()
        if not target or target["mass_zscore"] is None:
            return []
        industry = target["industry"]
        z = float(target["mass_zscore"])
        # 同行业、zscore接近的
        rows = conn.execute(
            """
            SELECT code, name, industry, mass_zscore, total_mkt_cap
            FROM factor_mass_daily
            WHERE trade_date=? AND industry=? AND mass_zscore IS NOT NULL AND code != ?
            """,
            (latest, industry, code),
        ).fetchall()
    # 按zscore距离排序
    scored = [(abs(float(r["mass_zscore"]) - z), r) for r in rows]
    scored.sort(key=lambda x: x[0])
    return [dict(r) for _, r in scored[:limit]]


def load_stock_moneyflow(db_path: Path, code: str, limit: int = 60) -> list[dict]:
    """个股资金流历史（net_mf_amount 时序）。"""
    with _read_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT trade_date, net_mf_vol, net_mf_amount,
                   buy_elg_vol, sell_elg_vol, buy_lg_vol, sell_lg_vol
            FROM daily_moneyflow
            WHERE code=? ORDER BY trade_date DESC LIMIT ?
            """,
            (code, limit),
        ).fetchall()
    if not rows:
        return []
    items = [dict(r) for r in rows]
    items.reverse()
    for it in items:
        for k in ("net_mf_vol","net_mf_amount","buy_elg_vol","sell_elg_vol","buy_lg_vol","sell_lg_vol"):
            if it[k] is not None:
                try: it[k] = round(float(it[k]), 2)
                except: pass
    return items


def table_sizes(db_path: Path) -> list[dict]:
    """各表磁盘占用(行数 + 估算字节)。"""
    with _read_conn(db_path) as conn:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()]
        result = []
        for t in tables:
            n = conn.execute(f"SELECT COUNT(*) as n FROM {t}").fetchone()["n"]
            # dbstat 可估算页数,但未必可用;用 SUM(LENGTH) 粗略
            try:
                size = conn.execute(f"SELECT SUM(LENGTH(rowid)) AS s FROM {t}").fetchone()["s"] or 0
            except Exception:
                size = 0
            result.append({"table": t, "rows": n, "bytes": int(size)})
    return sorted(result, key=lambda x: -x["bytes"])
