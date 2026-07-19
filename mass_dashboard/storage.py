#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional, Sequence

import pandas as pd

MASS_COLUMNS = [
    "trade_date",
    "code",
    "name",
    "industry",
    "total_mkt_cap",
    "pe",
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
            """
        )


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
) -> pd.DataFrame:
    params: list[object] = [start_date, end_date]
    clauses = ["trade_date BETWEEN ? AND ?"]
    if codes:
        code_list = [str(code) for code in codes]
        frames = []
        with _read_conn(db_path) as conn:
            for part in chunked(code_list):
                placeholders = ",".join(["?"] * len(part))
                sql = f"""
                    SELECT code, trade_date, high, low
                    FROM daily_bars
                    WHERE trade_date BETWEEN ? AND ? AND code IN ({placeholders})
                    ORDER BY code, trade_date
                """
                frames.append(pd.read_sql_query(sql, conn, params=[start_date, end_date] + part))
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["code", "trade_date", "high", "low"])

    with _read_conn(db_path) as conn:
        return pd.read_sql_query(
            f"""
            SELECT code, trade_date, high, low
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
