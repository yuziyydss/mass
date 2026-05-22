#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

TOKEN_ENV_KEYS = ("TUSHARE_TOKEN", "TS_TOKEN", "TUSHARE_PRO_TOKEN")


def parse_env_file(env_file: Path) -> dict[str, str]:
    if not env_file.exists() or not env_file.is_file():
        return {}

    values: dict[str, str] = {}
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
        value = raw_value.strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1].strip()
        elif "#" in value:
            value = value.split("#", 1)[0].strip()
        values[key] = value
    return values


def first_config_value(env: dict[str, str], keys: tuple[str, ...], default: Optional[str] = None) -> Optional[str]:
    for key in keys:
        if os.getenv(key):
            return os.getenv(key)
        if env.get(key):
            return env[key]
    return default


@dataclass(frozen=True)
class AppConfig:
    root_dir: Path
    env_file: Path
    data_dir: Path
    exports_dir: Path
    db_path: Path
    host: str
    port: int
    run_time: str
    timezone: str
    app_username: str
    app_password: str
    tushare_token: Optional[str]
    quality_min_rows: int
    alert_webhook_url: str
    alert_webhook_type: str
    goldman_dir: Path

    @classmethod
    def load(
        cls,
        env_file: str = ".env",
        host: Optional[str] = None,
        port: Optional[int] = None,
        run_time: Optional[str] = None,
    ) -> "AppConfig":
        root_dir = Path(__file__).resolve().parents[1]
        env_path = Path(env_file)
        if not env_path.is_absolute():
            env_path = root_dir / env_path
        env = parse_env_file(env_path)

        data_dir = Path(first_config_value(env, ("MASS_DATA_DIR",), str(root_dir / "dashboard_data")) or "")
        if not data_dir.is_absolute():
            data_dir = root_dir / data_dir

        exports_dir = Path(first_config_value(env, ("MASS_EXPORTS_DIR",), str(root_dir / "factors")) or "")
        if not exports_dir.is_absolute():
            exports_dir = root_dir / exports_dir

        goldman_dir = Path(first_config_value(env, ("GOLDMAN_DATA_DIR",), str(exports_dir / "goldman")) or "")
        if not goldman_dir.is_absolute():
            goldman_dir = root_dir / goldman_dir

        db_path = Path(first_config_value(env, ("MASS_DB_PATH",), str(data_dir / "mass_dashboard.db")) or "")
        if not db_path.is_absolute():
            db_path = root_dir / db_path

        token = first_config_value(env, TOKEN_ENV_KEYS)
        resolved_host = host or first_config_value(env, ("DASHBOARD_HOST",), "127.0.0.1") or "127.0.0.1"
        resolved_port = int(port or first_config_value(env, ("DASHBOARD_PORT",), "8008") or "8008")

        return cls(
            root_dir=root_dir,
            env_file=env_path,
            data_dir=data_dir,
            exports_dir=exports_dir,
            db_path=db_path,
            host=resolved_host,
            port=resolved_port,
            run_time=run_time or first_config_value(env, ("MASS_RUN_TIME", "RUN_TIME"), "18:30") or "18:30",
            timezone=first_config_value(env, ("TIMEZONE",), "Asia/Shanghai") or "Asia/Shanghai",
            app_username=first_config_value(env, ("APP_USERNAME",), "admin") or "admin",
            app_password=first_config_value(env, ("APP_PASSWORD",), "") or "",
            tushare_token=token,
            quality_min_rows=int(first_config_value(env, ("MASS_QUALITY_MIN_ROWS",), "4000") or "4000"),
            alert_webhook_url=first_config_value(env, ("MASS_ALERT_WEBHOOK_URL", "FEISHU_WEBHOOK_URL"), "") or "",
            alert_webhook_type=first_config_value(env, ("MASS_ALERT_WEBHOOK_TYPE",), "feishu") or "feishu",
            goldman_dir=goldman_dir,
        )
